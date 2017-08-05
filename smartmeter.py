#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import sys
import serial
import logging
import ConfigParser
import traceback
import os
import time
from datadog import statsd


class EchonetLite(object):
    # XXX 未使用部分はコード定義未作成
    class EHD:
        EHD1     = '\x10'
        EHD2_TY1 = '\x81'
        EHD2_TY2 = '\x82'

    class ESV:
        SET_I    = '\x60'
        SET_C    = '\x61'
        GET      = '\x62'
        INF_REQ  = '\x63'
        SET_GET  = '\x6E'
        GET_RES  = '\x72'

    class PDC:
        GET_REQ  = '\x00'

    class CLS_GRP:
        SENSOR   = '\x00'
        AIRCON   = '\x01'
        FACILITY = '\x02'
        COOKING  = '\x03'
        HEALTH   = '\x04'
        MANAGE   = '\x05'
        AV       = '\x06'
        PROFILE  = '\x0E'
        USERDEF  = '\x0F'

    class EPC:
        TOTAL_PW = '\xE0'
        CUR_PW   = '\xE7'

    @staticmethod
    def smart_meter():
        return "".join([EchonetLite.CLS_GRP.FACILITY, '\x88\x01'])

    @staticmethod
    def message():
        mes = [
                EchonetLite.EHD.EHD1,
                EchonetLite.EHD.EHD2_TY1,
                "\x00\x01",      # TID (参考:EL p.3-3)
                EchonetLite.CLS_GRP.MANAGE,
                "\xFF\x01",
                EchonetLite.smart_meter(),
                EchonetLite.ESV.GET,
                "\x01",          # OPC(1個)(参考:EL p.3-7)
                EchonetLite.EPC.CUR_PW,
                EchonetLite.PDC.GET_REQ
        ]
        return "".join(mes)

    @staticmethod
    def parse(line):
        return EchonetLite.Response(line)

    class Response:
        def __init__(self, line):
            self._line = line
            if line.startswith("ERXUDP"):
                self._res = line.split(' ')[-1]

        def is_valid_response(self):
            if hasattr(self, '_res'):
                return False
            elif self._res is None:
                return False
            elif not (self.seoj == EchonetLite.smart_meter() and self.esv == EchonetLite.ESV.GET_RES):
                return False
            return True

        @property
        def seoj(self):
            return self._res[8:8+6].decode('hex')

        @property
        def esv(self):
            return self._res[20:20+2].decode('hex')

        @property
        def epc(self):
            return self._res[24:24+2].decode('hex')

        @property
        def value(self):
            return int(self._res[-8:], 16)


class WiSunDevice(object):
    MAX_ROW = 100

    def __init__(self, serialPortDev, baudrate=115200):
        self.log = logging.getLogger(__name__)
        self._ser = serial.Serial(serialPortDev, baudrate)
        self._ser.timeout = 2
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        self._ser.readline()
        self._ser.timeout = 60
        self.pan_info = {}

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self._timeout = value
        self.log.debug("set timeout=%d" % value)
        self._ser.timeout = value

    def skinfo(self):
        return self._command('SKINFO')

    def skreset(self):
        return self._command('SKRESET')

    def skscan(self, channel_mask="FFFFFFFF", duration=6):
        self._command("SKSCAN 2 %s %d 0" % (channel_mask, duration))
        result = self._response_lines('EVENT 22')
        for line in result.split('\n'):
            cols = line.split(':')
            if len(cols) == 2:
                self.pan_info[cols[0]] = cols[1]

    def skjoin(self):
        self._command("SKJOIN %s" % self.pan_ipv6addr)
        return self._response_lines('EVENT 25', 'EVENT 24')

    def sksetpwd(self, password):
        return self._command('SKSETPWD C %s' % password)

    def sksendto(self, data):
        self._command("SKSENDTO 1 {0} 0E1A 1 0 {1:04X} {2}".format(
            self.pan_ipv6addr,
            len(data),
            data
        ))
        for i in range(0, self.MAX_ROW):
            line = self._ser.readline().strip()
            if len(line) > 0:
                if line.startswith('ERXUDP'):
                    self.log.debug(line)
                    break
        return line

    def sksetrbid(self, rbid):
        return self._command('SKSETRBID %s' % rbid)

    def skver(self):
        return self._command('SKVER')

    def set_pan_settings(self, rbid, rbpwd):
        self.sksetpwd(rbpwd)
        self.sksetrbid(rbid)
        for i in range(0, 5):
            self.skscan()
            if 'Channel' in self.pan_info:
                break
        else:
            self.log.error('Cannot get pan_info')
            sys.exit(1)
        self._command('SKSREG S2 %s' % self.pan_info['Channel'])
        self._command('SKSREG S3 %s' % self.pan_info['Pan ID'])

        # XXX netaddrでipv6変換すると動かなかった… 0を詰めるとNGぽい
        self._ser.write('SKLL64 %s\r\n' % self.pan_info['Addr'])
        self.pan_ipv6addr = self._ser.readline().strip()
        self.log.info('Connect to { Channel: %s, Pan ID: %s, IPv6Addr: %s }' % (self.pan_info['Channel'], self.pan_info['Pan ID'], self.pan_ipv6addr))

    def polling_power_consumption(self):
        while True:
            try:
                yield int(wsdev._get_current_power_consumption())
                errcnt = 0
                time.sleep(interval)
            except KeyboardInterrupt:
                break
            except StandardError:
                self.log.error(traceback.print_exc())
                errcnt += 1
                if errcnt > 100:
                    break

    def close(self):
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        self._ser.close()
        self.log.info('close connection')

    def _get_current_power_consumption(self):
        data = EchonetLite.message()
        for i in range(0, self.MAX_ROW):
            resp = EchonetLite.parse(self.sksendto(data))

            if resp.is_valid_response() and resp.epc == EchonetLite.EPC.CUR_PW:
                self.log.info(u"瞬時電力計測値:{0}[W]".format(resp.value))
                return resp.value

    def _command(self, command):
        self.log.debug("%s:" % command)
        self._ser.write("%s\r\n" % command)
        return self._response_lines()

    def _response_lines(self, term_word="OK", fail_word="FAIL"):
        lines = []
        for i in range(0, self.MAX_ROW):
            line = self._ser.readline().strip()
            if len(line) > 0:
                if line.startswith(term_word):
                    self.log.debug(line)
                    break
                elif line.startswith(fail_word):
                    self.log.error(line)
                    raise StandardError
                else:
                    self.log.debug(line)
                    lines.append(line)
        return "\n".join(lines).strip()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    inifile = ConfigParser.SafeConfigParser()
    inifile.read(os.path.normpath(os.path.join(os.path.abspath(__file__), '../config.ini')))

    wsdev = WiSunDevice(inifile.get('General', 'com_port'))
    wsdev.timeout = 2
    wsdev.log.info('SKVER: %s' % wsdev.skver())
    wsdev.timeout = 60

    wsdev.set_pan_settings(inifile.get('RouteB', 'rbid'), inifile.get('RouteB', 'rbpwd'))
    wsdev.skjoin()

    wsdev.timeout = 2

    errcnt = 0
    try:
        interval = int(inifile.get('General', 'interval'))
    except ValueError:
        interval = 10
        wsdev.log.warn('invalid interval %s. use default interval: 10' % inifile.get('General', 'interval'))

    for power in wsdev.polling_power_consumption():
        statsd.gauge('power', power)

    # TODO systemd終了時に実行できないので、やり方を考える
    wsdev.close()
    sys.exit(0)
