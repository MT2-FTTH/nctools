#!/usr/bin/python
##############################################################################
#                                                                            #
#  ncproxy.py                                                                #
#                                                                            #
#  History Change Log:                                                       #
#                                                                            #
#    1.0  [SW]  2017/09/04    first version                                  #
#    1.1  [SW]  2017/09/05    improved logging, patching, auto-responses     #
#    1.2  [SW]  2017/10/01    add support patch-files                        #
#    1.3  [SW]  2017/12/01    propagate auth-errors, fix regexp              #
#    1.4  [SB]  2018/10/06    add public key support                         #
#                                                                            #
#  Objective:                                                                #
#    ncproxy is a transparent logging proxy for NETONF over SSH              #
#                                                                            #
#  License:                                                                  #
#    Licensed under the BSD license                                          #
#    See LICENSE.md delivered with this project for more information.        #
#                                                                            #
#  Authors:                                                                  #
#    Sven Wisotzky                                                           #
#    mail:  sven.wisotzky(at)nokia.com                                       #
#                                                                            #
#                                           (c) 2017 by Sven Wisotzky, Nokia #
#                                                                            #
#    Stephane Bryant                                                         #
#    mail:  stephane.bryant(at)mt2.fr                                        #
#                                                                            #
#                                           (c) 2018 by Stephane Bryant, MT2 #
##############################################################################

"""
NETCONF proxy in Python Version 1.4
Copyright (C) 2015-2017 Nokia. All Rights Reserved.
Copyright (C) 2018 MT2. All Rights Reserved
"""

import binascii
import logging
import os
import paramiko
import socket
import sys
import threading
import time
import traceback
import argparse
import json
import re

if sys.version_info > (3,):
    from urllib.parse import urlparse
else:
    from urlparse import urlparse

__title__ = "ncproxy"
__version__ = "1.4"
__status__ = "released"
__author__ = "Stephane Bryant"
__date__ = "2018 July 10"


class ncHandler(paramiko.SubsystemHandler):

    def __init__(self, channel, name, server, srv_transport):
        paramiko.SubsystemHandler.__init__(self, channel, name, server)
        self.srv_transport = srv_transport

    def start_subsystem(self, name, transport, channel):
        try:
            srv_channel = self.srv_transport.open_session()
            srv_channel.invoke_subsystem('netconf')

        except Exception as e:
            # --- close channel/transport to NETCONF client ------------------
            log.warning('NETCONF over SSH to %s failed: %s', url.hostname, str(e))
            channel.close()
            transport.close()
            return

        log.info('NETCONF messaging capture')

        nccbuf = ""
        srvbuf = ""

        while transport.is_active():
            # --- receive bytes from server, append to srvbuf ----------------

            while srv_channel.recv_ready():
                srvbuf += srv_channel.recv(65535)

            # --- extract srvmsgs[] from srvbuf ------------------------------

            srvmsgs = []
            if len(srvbuf) > 4:
                if srvbuf[0:2] != "\n#":
                    base10 = True   # --- base:1.0 framing (EOM) -------------
                    srvmsgs = srvbuf.split("]]>]]>")
                    srvbuf = srvmsgs.pop()
                else:
                    base10 = False  # --- base:1.1 framing (chunks) ----------

                    tmp = ""
                    pos = 0

                    while pos < len(srvbuf) and len(srvbuf) > 4:
                        if srvbuf[pos:pos + 4] == "\n##\n":
                            srvmsgs.append(tmp)
                            tmp = ""
                            srvbuf = srvbuf[pos + 4:]
                            pos = 0
                        elif srvbuf[pos:pos + 2] == "\n#":
                            idx = srvbuf.find("\n", pos + 2)
                            if idx != -1:
                                bytes = int(srvbuf[pos + 2:idx])
                                tmp += srvbuf[idx + 1:idx + 1 + bytes]
                                pos = idx + 1 + bytes
                            else:
                                # --- need to wait for more bytes to come ----
                                break
                        else:
                            log.error('SERVER FRAMING ERROR')
                            srvbuf = ""
                            break

            # --- patch, forward, print NETCONF server messages: srvmsgs[] ---
            for msg in srvmsgs:
                for rule in rules['server-msg-modifier']:
                    msg = rule['regex'].sub(rule['patch'], msg)

                if not base10:
                    buf = "\n#%d\n" % len(msg)
                    channel.send(buf)
                    serverlog.write(buf)

                pos = 0
                while pos < len(msg):
                    if pos + 16384 < len(msg):
                        buf = msg[pos:pos + 16384]
                        pos += 16384
                    else:
                        buf = msg[pos:]
                        pos = len(msg)
                    channel.send(buf)
                    serverlog.write(buf)

                if base10:
                    buf = "]]>]]>"
                else:
                    buf = "\n##\n"
                channel.send(buf)
                serverlog.write(buf)
                serverlog.flush()

            # --- receive bytes from client, append to nccbuf ----------------

            while channel.recv_ready():
                nccbuf += channel.recv(65535)

            # --- extract nccmsgs[] from nccbuf ------------------------------

            nccmsgs = []
            if len(nccbuf) > 4:
                if nccbuf[0:2] != "\n#":
                    base10 = True   # --- base:1.0 framing (EOM) -------------
                    nccmsgs = nccbuf.split("]]>]]>")
                    nccbuf = nccmsgs.pop()
                else:
                    base10 = False  # --- base:1.1 framing (chunks) ----------

                    tmp = ""
                    pos = 0

                    while pos < len(nccbuf) and len(nccbuf) > 4:
                        if nccbuf[pos:pos + 4] == "\n##\n":
                            nccmsgs.append(tmp)
                            tmp = ""
                            nccbuf = nccbuf[pos + 4:]
                            pos = 0
                        elif nccbuf[pos:pos + 2] == "\n#":
                            idx = nccbuf.find("\n", pos + 2)
                            if idx != -1:
                                bytes = int(nccbuf[pos + 2:idx])
                                tmp += nccbuf[idx + 1:idx + 1 + bytes]
                                pos = idx + 1 + bytes
                            else:
                                # --- need to wait for more bytes to come ----
                                break
                        else:
                            log.error('CLIENT FRAMING ERROR')
                            nccbuf = ""
                            break

            # --- patch, forward, print NETCONF client messages: nccmsgs[] ---
            for msg in nccmsgs:
                for rule in rules['client-msg-modifier']:
                    msg = rule['regex'].sub(rule['patch'], msg)

                sendmsg = True
                for rule in rules['auto-respond']:
                    if rule['regex'].match(msg):
                        log.info('Auto-response to NETCONF client message')
                        tmp = rule['regex'].sub(rule['response'], msg)
                        if base10:
                            srvbuf += tmp
                            srvbuf += "]]>]]>"
                        else:
                            srvbuf += "\n#%d\n" % len(tmp)
                            srvbuf += tmp
                            srvbuf += "\n##\n"
                        sendmsg = False
                        break

                if not base10:
                    buf = "\n#%d\n" % len(msg)
                    if sendmsg:
                        srv_channel.send(buf)
                    clientlog.write(buf)

                pos = 0
                while pos < len(msg):
                    if pos + 16384 < len(msg):
                        buf = msg[pos:pos + 16384]
                        pos += 16384
                    else:
                        buf = msg[pos:]
                        pos = len(msg)
                    if sendmsg:
                        srv_channel.send(buf)
                    clientlog.write(buf)

                if base10:
                    buf = "]]>]]>"
                else:
                    buf = "\n##\n"
                if sendmsg:
                    srv_channel.send(buf)
                clientlog.write(buf)
                clientlog.flush()

            if srv_channel.exit_status_ready():
                break
            if channel.exit_status_ready():
                break
            time.sleep(0.01)

        else:
            serverlog.flush()
            clientlog.flush()
            log.info('NETCONF communication finished')

        if srv_channel.exit_status_ready():
            log.warning("Connection closed by peer; server down")
        if channel.exit_status_ready():
            log.warning("Connection closed by peer; client down")

        # --- close channel/transport to NETCONF server ----------------------
        srv_channel.close()
        self.srv_transport.close()


class ssh_server(paramiko.ServerInterface):

    def __init__(self):
        log.debug("ssh_server.__init__()")
        self.event = threading.Event()

    def check_channel_request(self, kind, chanid):
        log.debug("ssh_server.check_channel_request(kind=%s, chanid=%s)",  kind, chanid)
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, username, password):
        log.debug("ssh_server.check_auth_password(username=%s, password=%s)", username, password)
        try:
            self.srv_tcpsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv_tcpsock.connect((url.hostname, url.port or 830))
            self.srv_transport = paramiko.Transport(self.srv_tcpsock)
            self.srv_transport.connect(hostkey=server_host_key, pkey=client_private_key,username=username, password=password)

        except Exception as e:
            # Should be either of the following:
            #   paramiko.BadHostKeyException
            #   paramiko.AuthenticationException
            #   paramiko.SSHException
            #   socket.error
            log.critical('Server session setup/authentication failed: %s', str(e))
            log.debug(''.join(traceback.format_exception(*sys.exc_info())))
            self.srv_transport.close()
            self.srv_tcpsock.close()
            return paramiko.AUTH_FAILED

        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username, key):
        log.debug("ssh_server.check_auth_publickey()")
        try:
            self.srv_tcpsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv_tcpsock.connect((url.hostname, url.port or 830))
            self.srv_transport = paramiko.Transport(self.srv_tcpsock)
            self.srv_transport.connect(hostkey=server_host_key, pkey=client_private_key,username=username)

        except Exception as e:
            # Should be either of the following:
            #   paramiko.BadHostKeyException
            #   paramiko.AuthenticationException
            #   paramiko.SSHException
            #   socket.error
            log.critical('Server session setup/authentication failed: %s', str(e))
            log.debug(''.join(traceback.format_exception(*sys.exc_info())))
            self.srv_transport.close()
            self.srv_tcpsock.close()
            return paramiko.AUTH_FAILED

        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        log.debug("ssh_server.get_allowed_auths(username=%s)", username)
        return 'publickey,password'

    def check_channel_shell_request(self, channel):
        log.debug("ssh_server.check_channel_shell_request()")
        log.critical('SHELL request is NOT supported')
        self.event.set()
        return False

    def check_channel_exec_request(self, channel, command):
        log.debug("ssh_server.check_channel_exec_request()")
        log.critical('EXEC request is NOT supported')
        return False

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        log.debug("ssh_server.check_channel_pty_request(term=%s, width=%d, height=%d)", term, width, height)
        log.critical('PTY request is NOT supported')
        return False

    def check_channel_subsystem_request(self, channel, name):
        log.debug("ssh_server.check_channel_subsystem_request(name=%s)", name)
        if name == 'netconf':
            handler = ncHandler(channel, name, self, self.srv_transport)
            handler.start()
            return True
        log.critical('Subsystem %s is NOT supported', name)
        return False


if __name__ == '__main__':
    prog = os.path.splitext(os.path.basename(sys.argv[0]))[0]

    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version', version=prog + ' ' + __version__)

    group = parser.add_argument_group()
    group.add_argument('-v', '--verbose', action='count', help='enable logging')
    group.add_argument('-d', '--debug', action='count', help='enable ssh-lib logging')
    group.add_argument('--logfile', metavar='filename', type=argparse.FileType('wb', 0), help='trace/debug log (default: <stderr>)')
    group.add_argument('--serverlog', metavar='filename', default='-', type=argparse.FileType('wb', 0), help='server log (default: <stdout>)')
    group.add_argument('--clientlog', metavar='filename', default='-', type=argparse.FileType('wb', 0), help='client log (default: <stdout>)')

    group = parser.add_argument_group()
    group.add_argument('--patch', metavar='filename', type=argparse.FileType('r'), help='Patch NETCONF messages (default: <none>)')

    group = parser.add_argument_group()
    group.add_argument("--clientprivatekey", metavar='filename', type=argparse.FileType('r'), help='client RSA private key file (default: <none>)')
    group.add_argument("--proxyhostkey", metavar='filename', type=argparse.FileType('r'), help='proxy private host key file (default: <none>)')
    group.add_argument('--proxyhostkeyalg', metavar='RSA ECDSA', default="RSA", type=str, help='proxy host key algorithm (default: <RSA>)')
    group.add_argument("--serverhostkey", metavar='filename', type=argparse.FileType('r'), help='server private host key file (default: <none>)')
    group.add_argument('--serverhostkeyalg', metavar='RSA ECDSA', default="RSA", type=str, help='server host key algorithm (default: <RSA>)')

    group = parser.add_argument_group()
    group.add_argument('--port', metavar='tcpport', type=int, default=830, help='TCP-port ncproxy is listening')
    group.add_argument('server', metavar='netconf://<hostname>[:port]', default="netconf://127.0.0.1:830", help='Netconf over SSH server')

    options = parser.parse_args()

    # --- setup module logging -----------------------------------------------
    if options.logfile is None:
        loghandler = logging.StreamHandler(sys.stderr)
    else:
        loghandler = logging.StreamHandler(options.logfile)
    timeformat = '%y/%m/%d %H:%M:%S'
    logformat = '%(asctime)s,%(msecs)-3d %(levelname)-8s %(message)s'
    loghandler.setFormatter(logging.Formatter(logformat, timeformat))

    log = logging.getLogger('paramiko')
    if options.debug is None:
        log.setLevel(logging.NOTSET)
        log.addHandler(logging.NullHandler())
    elif options.debug == 1:
        log.setLevel(logging.CRITICAL)
        log.addHandler(loghandler)
    elif options.debug == 2:
        log.setLevel(logging.ERROR)
        log.addHandler(loghandler)
    elif options.debug == 3:
        log.setLevel(logging.WARNING)
        log.addHandler(loghandler)
    elif options.debug == 4:
        log.setLevel(logging.INFO)
        log.addHandler(loghandler)
    else:
        log.setLevel(logging.DEBUG)
        log.addHandler(loghandler)

    log = logging.getLogger('ncproxy')
    if options.verbose is None:
        log.setLevel(logging.NOTSET)
        log.addHandler(logging.NullHandler())
    elif options.verbose == 1:
        log.setLevel(logging.CRITICAL)
        log.addHandler(loghandler)
    elif options.verbose == 2:
        log.setLevel(logging.ERROR)
        log.addHandler(loghandler)
    elif options.verbose == 3:
        log.setLevel(logging.WARNING)
        log.addHandler(loghandler)
    elif options.verbose == 4:
        log.setLevel(logging.INFO)
        log.addHandler(loghandler)
    else:
        log.setLevel(logging.DEBUG)
        log.addHandler(loghandler)

    # --- set server/client log ----------------------------------------------
    serverlog = options.serverlog
    clientlog = options.clientlog

    # --- parse server URL ---------------------------------------------------
    if options.server.find('://') == -1:
        url = urlparse("netconf://" + options.server)
    else:
        url = urlparse(options.server)

    if url.scheme != "netconf":
        log.critical('Connection to NETCONF server(s) only')
        sys.exit(1)

    # --- parse server URL ---------------------------------------------------
    if options.patch:
        rules = json.load(options.patch)
        for rule in rules['server-msg-modifier']:
            if rule.has_key('patch-file'):
                with open(rule['patch-file'], 'r') as file:
                    rule['patch'] = file.read()
            rule['regex'] = re.compile(rule['match'], re.DOTALL)

        for rule in rules['client-msg-modifier']:
            if rule.has_key('patch-file'):
                with open(rule['patch-file'], 'r') as file:
                    rule['patch'] = file.read()
            rule['regex'] = re.compile(rule['match'], re.DOTALL)

        for rule in rules['auto-respond']:
            if rule.has_key('response-file'):
                with open(rule['response-file'], 'r') as file:
                    rule['response'] = file.read()
            rule['regex'] = re.compile(rule['match'], re.DOTALL)
    else:
        rules = {}
        rules['server-msg-modifier'] = []
        rules['client-msg-modifier'] = []
        rules['auto-respond'] = []

    # --- waiting for incoming client connections ----------------------------
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(None)
        sock.bind(('', options.port))
        sock.listen(100)
        log.info('Listening for client connection ...')
    except Exception as e:
        log.critical('Server setup failed: %s', str(e))
        log.debug(''.join(traceback.format_exception(*sys.exc_info())))
        sys.exit(1)

    # --- client private key -------------------------------------------------
    client_private_key = None
    if options.clientprivatekey is not None:
        client_private_key = paramiko.RSAKey.from_private_key_file(options.clientprivatekey.name)
        log.debug('client private key: %s', binascii.hexlify(client_private_key.get_fingerprint()))

    # --- server host key
    server_host_key = None
    if options.serverhostkey is not None:
        if options.serverhostkeyalg == "ECDSA":
            server_host_key = paramiko.ECDSAKey.from_private_key_file(options.serverhostkey.name)
        else:
            server_host_key = paramiko.RSAKey.from_private_key_file(options.serverhostkey.name)
        log.debug('server host Key: %s', binascii.hexlify(server_host_key.get_fingerprint()))

    # --- proxy host key
    if options.proxyhostkey is None:
        log.debug('Generating new host key')
        proxy_host_key = paramiko.RSAKey.generate(2048)
    else:
        if options.proxyhostkeyalg == "ECDSA":
            proxy_host_key = paramiko.ECDSAKey.from_private_key_file(options.proxyhostkey.name)
        else:
            proxy_host_key = paramiko.RSAKey.from_private_key_file(options.proxyhostkey.name)
    log.debug('proxy host Key: %s', binascii.hexlify(proxy_host_key.get_fingerprint()))

    # --- handler for incoming client connections ----------------------------
    while True:
        try:
            client, addr = sock.accept()
            log.info("Incoming client connection from %s (srcport: %d)", addr[0], addr[1])
        except (KeyboardInterrupt, SystemExit):
            log.info('ncproxy terminated by user')
            sys.exit(1)
        except Exception as e:
            log.critical('Server listen failure: %s', str(e))
            log.debug(''.join(traceback.format_exception(*sys.exc_info())))
            sys.exit(1)

        try:
            t = paramiko.Transport(client)
            t.load_server_moduli()
            t.add_server_key(proxy_host_key)
            t.set_subsystem_handler('netconf', ncHandler)
            t.start_server(server=ssh_server())
        except Exception as e:
            log.warning('Connection failed: %s', str(e))

# EOF
