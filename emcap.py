#!/usr/bin/python2

from gnuradio import blocks
from gnuradio import eng_notation
from gnuradio import gr
from gnuradio import uhd
from gnuradio.eng_option import eng_option
from gnuradio.filter import firdes
from time import sleep
from threading import Thread
from datetime import datetime
from sigmf.sigmffile import SigMFFile
from dsp import butter_filter
from socketwrapper import SocketWrapper
from traceset import TraceSet
from scipy.signal import hilbert
from scipy import fftpack
import matplotlib.pyplot as plt
import numpy as np
import time
import sys
import socket
import os
import signal
import logging
import struct
import binascii
import osmosdr
import argparse
import serial
import pickle
import zlib
import subprocess

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger(__name__)

hilbert3 = lambda x: hilbert(x, fftpack.next_fast_len(len(x)))[:len(x)]


def reset_usrp():
    print("Resetting USRP")
    p = subprocess.Popen(["/usr/lib/uhd/utils/b2xx_fx3_utils", "--reset-device"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(p.communicate())


def handler(signum, frame):
    print("Got CTRL+C")
    exit(0)

signal.signal(signal.SIGINT, handler)

def binary_to_hex(binary):
    result = []
    for elem in binary:
        result.append("{:0>2}".format(binascii.hexlify(elem)))
    return ' '.join(result)

class CtrlPacketType:
    SIGNAL_START = 0
    SIGNAL_END = 1

class InformationElementType:
    PLAINTEXT = 0
    KEY = 1
    CIPHERTEXT = 2
    MASK = 3


def set_gain(source, gain):
    source.set_gain(gain, 0)
    new_gain = source.get_gain()
    if new_gain != gain:
        raise Exception("Requested gain %.2f but set gain %.2f" % (gain, new_gain))
    return True


# SDR capture device
class SDR(gr.top_block):
    def __init__(self, hw="usrp", samp_rate=100000, freq=3.2e9, gain=0, ds_mode=False, agc=False):
        gr.enable_realtime_scheduling()
        gr.top_block.__init__(self, "SDR capture device")

        ##################################################
        # Variables
        ##################################################
        self.hw = hw
        self.samp_rate = samp_rate
        self.freq = freq
        self.gain = gain
        self.ds_mode = ds_mode
        logger.info("%s: samp_rate=%d, freq=%f, gain=%d, ds_mode=%s" % (hw, samp_rate, freq, gain, ds_mode))

        ##################################################
        # Blocks
        ##################################################
        if hw == "usrp":
            self.sdr_source = uhd.usrp_source(
               ",".join(("", "recv_frame_size=1024", "num_recv_frames=1024", "spp=1024")),
               #",".join(("", "")),
               uhd.stream_args(
               cpu_format="fc32",
               channels=range(1),
               ),
            )
            self.sdr_source.set_samp_rate(samp_rate)
            self.sdr_source.set_center_freq(freq, 0)
            set_gain(self.sdr_source, gain)
            # self.sdr_source.set_min_output_buffer(16*1024*1024)  # 16 MB output buffer
            self.sdr_source.set_antenna('RX2', 0)
            self.sdr_source.set_bandwidth(samp_rate, 0)
            self.sdr_source.set_recv_timeout(0.001, True)
        else:
            if hw == "hackrf":
                rtl_string = ""
            else:
                rtl_string = "rtl=0,"
            if ds_mode:
                self.sdr_source = osmosdr.source(args="numchan=" + str(1) + " " + rtl_string + "buflen=1024,direct_samp=2")
            else:
                self.sdr_source = osmosdr.source(args="numchan=" + str(1) + " " + rtl_string + "buflen=4096")
            self.sdr_source.set_sample_rate(samp_rate)
            self.sdr_source.set_center_freq(freq, 0)
            self.sdr_source.set_freq_corr(0, 0)
            self.sdr_source.set_dc_offset_mode(0, 0)
            self.sdr_source.set_iq_balance_mode(0, 0)
            if agc:
                self.sdr_source.set_gain_mode(True, 0)
            else:
                self.sdr_source.set_gain_mode(False, 0)
                # self.sdr_source.set_if_gain(24, 0)
                # self.sdr_source.set_bb_gain(20, 0)
                set_gain(self.sdr_source, gain)
            self.sdr_source.set_antenna('', 0)
            self.sdr_source.set_bandwidth(samp_rate, 0)

        self.udp_sink = blocks.udp_sink(8, "127.0.0.1", 3884, payload_size=1472, eof=True)

        ##################################################
        # Connections
        ##################################################
        self.connect((self.sdr_source, 0), (self.udp_sink, 0))

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        if self.hw == "usrp":
            self.sdr_source.set_samp_rate(self.samp_rate)
        else:
            self.sdr_source.set_sample_rate(self.sample_rate)

    def get_freq(self):
        return self.freq

    def set_freq(self, freq):
        self.freq = freq
        self.sdr_source.set_center_freq(self.freq, 0)

    def get_gain(self):
        return self.gain

    def set_gain(self, gain):
        self.gain = gain
        self.sdr_source.set_gain(self.gain, 0)

class TTYWrapper(Thread):
    def __init__(self, port, cb_pkt):
        Thread.__init__(self)
        self.setDaemon(True)
        self.port = port
        logger.debug("Connecting to %s" % str(port))
        self.s = serial.Serial(port, 115200)
        self.cb_pkt = cb_pkt
        self.data = b""

    def _parse(self, client_socket, client_address):
        bytes_parsed = self.cb_pkt(client_socket, client_address, self.data)
        self.data = self.data[bytes_parsed:]

    def recv(self):
        receiving = True
        while receiving:
            if self.s.is_open:
                chunk = self.s.read(1)
                self.data += chunk
            else:
                receiving = False
                logger.debug("Serial connection is closed, stopping soon!")

            self._parse(self.s, None)

    def run(self):
        self.recv()

class CtrlType:
    DOMAIN = 0
    UDP = 1
    SERIAL = 2

# EMCap class: wait for signal and start capturing using a SDR
class EMCap():
    def __init__(self, cap_kwargs={}, kwargs={}, ctrl_socket_type=None):
        # Set up data socket
        self.data_socket = SocketWrapper(socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM), ('127.0.0.1', 3884), self.cb_data)
        self.online = kwargs['online']

        # Set up sockets
        self.ctrl_socket_type = ctrl_socket_type
        if ctrl_socket_type == CtrlType.DOMAIN:
            unix_domain_socket = '/tmp/emma.socket'
            self.clear_domain_socket(unix_domain_socket)
            self.ctrl_socket = SocketWrapper(socket.socket(family=socket.AF_UNIX, type=socket.SOCK_STREAM), unix_domain_socket, self.cb_ctrl)
        elif ctrl_socket_type == CtrlType.UDP:
            self.ctrl_socket = SocketWrapper(socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM), ('172.18.15.21', 3884), self.cb_ctrl)
        elif ctrl_socket_type == CtrlType.SERIAL:
            self.ctrl_socket = TTYWrapper("/dev/ttyUSB0", self.cb_ctrl)
        else:
            logger.error("Unknown ctrl_socket_type")
            exit(1)

        if not self.online is None:
            try:
                self.emma_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.emma_client.connect((self.online, 3885))
            except Exception as e:
                print(e)
                exit(1)

        self.sdr = SDR(**cap_kwargs)
        self.cap_kwargs = cap_kwargs
        self.kwargs = kwargs
        self.store = False
        self.stored_plaintext = []
        self.stored_key = []
        self.stored_data = []
        self.trace_set = []
        self.plaintexts = []
        self.keys = []
        self.online_counter = 0
        self.limit_counter = 0
        self.limit = kwargs['limit']
        #self.manifest = kwargs['manifest']
        self.compress = kwargs['compress']
        if self.sdr.hw == 'usrp':
            self.wait_num_chunks = 0
        else:
            self.wait_num_chunks = 50  # Bug in rtl-sdr?

        self.global_meta = {
            "core:datatype": "cf32_le",
            "core:version": "0.0.1",
            "core:license": "CC0",
            "core:hw": self.sdr.hw,
            "core:sample_rate": self.sdr.samp_rate,
            "core:author": "Pieter Robyns"
        }

        self.capture_meta = {
            "core:sample_start": 0,
            "core:frequency": self.sdr.freq,
            "core:datetime": str(datetime.utcnow()),
        }

    def clear_domain_socket(self, address):
        try:
            os.unlink(address)
        except OSError:
            if os.path.exists(address):
                raise

    def cb_timeout(self):
        logger.warning("Timeout on capture, skipping...")
        self.sdr.stop()

    def cb_data(self, client_socket, client_address, data):
        self.stored_data.append(data)
        return len(data)

    def cb_ctrl(self, client_socket, client_address, data):
        logger.log(logging.NOTSET, "Control packet: %s" % binary_to_hex(data))
        if len(data) < 5:
            # Not enough for TLV
            return 0
        else:
            pkt_type, payload_len = struct.unpack(">BI", data[0:5])
            payload = data[5:]
            if len(payload) < payload_len:
                return 0  # Not enough for payload
            else:
                self.process_ctrl_packet(pkt_type, payload)
                # Send ack
                if self.ctrl_socket_type == CtrlType.SERIAL:
                    client_socket.write(b"k")
                else:
                    client_socket.sendall("k")
                return payload_len + 5

    def parse_ies(self, payload):
        while len(payload) >= 5:
            # Extract IE header
            ie_type, ie_len = struct.unpack(">BI", payload[0:5])
            payload = payload[5:]

            # Extract IE data
            ie = payload[0:ie_len]
            payload = payload[ie_len:]
            logger.debug("IE type %d of len %d: %s" % (ie_type, ie_len, binary_to_hex(ie)))

            # Determine what to do with IE
            if ie_type == InformationElementType.PLAINTEXT:
                self.stored_plaintext = [ord(c) for c in ie]
            elif ie_type == InformationElementType.KEY:
                self.stored_key = [ord(c) for c in ie]
            else:
                logger.warning("Unknown IE type: %d" % ie_type)

    def process_ctrl_packet(self, pkt_type, payload):
        if pkt_type == CtrlPacketType.SIGNAL_START:
            logger.debug("Starting for payload: %s" % binary_to_hex(payload))
            self.parse_ies(payload)
            self.sdr.start()

            # Spinlock until data
            timeout = 3
            current_time = 0.0
            while len(self.stored_data) <= self.wait_num_chunks:
                sleep(0.0001)
                current_time += 0.0001
                if current_time >= timeout:
                    logger.warning("Timeout while waiting for data. Did the SDR crash? Reinstantiating...")
                    del self.sdr
                    self.data_socket.socket.close()
                    self.data_socket = SocketWrapper(socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM), ('127.0.0.1', 3884), self.cb_data)
                    self.data_socket.start()
                    self.sdr = SDR(**self.cap_kwargs)
                    self.process_ctrl_packet(pkt_type, payload)
        elif pkt_type == CtrlPacketType.SIGNAL_END:
            # self.sdr.sdr_source.stop()
            self.sdr.stop()
            self.sdr.wait()

            logger.debug("Stopped after receiving %d chunks" % len(self.stored_data))
            #sleep(0.5)
            #logger.debug("After sleep we have %d chunks" % len(self.stored_data))

            # Successful capture (no errors or timeouts)
            if len(self.stored_data) > 0:  # We have more than 1 chunk
                # Data to file
                np_data = np.fromstring(b"".join(self.stored_data), dtype=np.complex64)
                self.trace_set.append(np_data)
                self.plaintexts.append(self.stored_plaintext)
                self.keys.append(self.stored_key)

                if len(self.trace_set) >= self.kwargs['traces_per_set']:
                    assert(len(self.trace_set) == len(self.plaintexts))
                    assert(len(self.trace_set) == len(self.keys))

                    np_trace_set = np.array(self.trace_set)
                    np_plaintexts = np.array(self.plaintexts, dtype=np.uint8)
                    np_keys = np.array(self.keys, dtype=np.uint8)

                    if not self.online is None: # Stream online
                        ts = TraceSet(name="online %d" % self.online_counter, traces=np_trace_set, plaintexts=np_plaintexts, ciphertexts=None, keys=np_keys)
                        logger.info("Pickling")
                        ts_p = pickle.dumps(ts)
                        logger.info("Size is %d" % len(ts_p))
                        stream_payload = ts_p
                        stream_payload_len = len(stream_payload)
                        logger.info("Streaming trace set of %d bytes to server" % stream_payload_len)
                        stream_hdr = struct.pack(">BI", 0, stream_payload_len)
                        self.emma_client.send(stream_hdr + stream_payload)
                        self.online_counter += 1
                    else: # Save to disk
                        if not self.kwargs['dry']:
                            # Write metadata to sigmf file
                            # if sigmf
                            #with open(test_meta_path, 'w') as f:
                            #    test_sigmf = SigMFFile(data_file=test_data_path, global_info=copy.deepcopy(self.global_meta))
                            #    test_sigmf.add_capture(0, metadata=capture_meta)
                            #    test_sigmf.dump(f, pretty=True)
                            # elif chipwhisperer:
                            logger.info("Dumping %d traces to file" % len(self.trace_set))
                            filename = str(datetime.utcnow()).replace(" ","_").replace(".","_")
                            output_dir = self.kwargs['output_dir']
                            np.save(os.path.join(output_dir, "%s_traces.npy" % filename), np_trace_set)  # TODO abstract this in trace_set class
                            np.save(os.path.join(output_dir, "%s_textin.npy" % filename), np_plaintexts)
                            np.save(os.path.join(output_dir, "%s_knownkey.npy" % filename), np_keys)
                            if self.compress:
                                logger.info("Calling emcap-compress...")
                                subprocess.call(['/usr/bin/python', 'emcap-compress.py', os.path.join(output_dir, "%s_traces.npy" % filename)])

                        self.limit_counter += len(self.trace_set)
                        if self.limit_counter >= self.limit:
                            print("Done")
                            exit(0)

                    # Clear results
                    self.trace_set = []
                    self.plaintexts = []
                    self.keys = []

                # Clear
                self.stored_data = []
                self.stored_plaintext = []

    def capture(self, to_skip=0, timeout=1.0):
        # Start listening for signals
        self.data_socket.start()
        self.ctrl_socket.start()

        # Wait until supplicant signals end of acquisition
        while self.ctrl_socket.is_alive():
            self.ctrl_socket.join(timeout=1.0)

        logging.info("Supplicant disconnected on control channel. Stopping...")


def main():
    parser = argparse.ArgumentParser(description='EMCAP')
    parser.add_argument('hw', type=str, choices=['usrp', 'hackrf', 'rtlsdr'], help='SDR capture hardware')
    parser.add_argument('ctrl', type=str, choices=['serial', 'udp'], help='Controller type')
    parser.add_argument('--sample-rate', type=int, default=4000000, help='Sample rate')
    parser.add_argument('--frequency', type=float, default=64e6, help='Capture frequency')
    parser.add_argument('--gain', type=float, default=50, help='RX gain')
    parser.add_argument('--traces-per-set', type=int, default=256, help='Number of traces per set')
    parser.add_argument('--limit', type=int, default=256*400, help='Limit number of traces')
    parser.add_argument('--output-dir', dest="output_dir", type=str, default="/run/media/pieter/ext-drive/em-experiments", help='Output directory to store samples')
    parser.add_argument('--online', type=str, default=None, help='Stream samples to remote EMMA instance at <IP address> for online processing.')
    parser.add_argument('--dry', default=False, action='store_true', help='Do not save to disk.')
    parser.add_argument('--ds-mode', default=False, action='store_true', help='Direct sampling mode.')
    parser.add_argument('--agc', default=False, action='store_true', help='Automatic Gain Control.')
    # parser.add_argument('--manifest', type=str, default=None, help='Capture manifest to use.')  # We now use --compress because no Tensorflow support in Python 2 and now GNU Radio support in Python 3.
    parser.add_argument('--compress', default=False, action='store_true', help='Compress using emcap-compress.')
    args, unknown = parser.parse_known_args()

    ctrl_type = None
    if args.ctrl == 'serial':
        ctrl_type = CtrlType.SERIAL
    elif args.ctrl == 'udp':
        ctrl_type = CtrlType.UDP

    e = EMCap(cap_kwargs={'hw': args.hw, 'samp_rate': args.sample_rate, 'freq': args.frequency, 'gain': args.gain, 'ds_mode': args.ds_mode, 'agc': args.agc}, kwargs=args.__dict__, ctrl_socket_type=ctrl_type)
    e.capture()


if __name__ == '__main__':
    main()
