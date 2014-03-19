#
# Copyright (c) 2012 Patrice Munger
# This file is part of pynetdicom, released under a modified MIT license.
#    See the file license.txt included with this distribution, also
#    available at http://pynetdicom.googlecode.com
#

"""
This module implements the DUL service provider, allowing a DUL service user to send and receive DUL messages.
The User and Provider talk to each other using a TCP socket. The DULServer runs in a thread,
so that and implements an event loop whose events will drive the state machine.
"""

from threading import Thread
import socket
import time
import os
import select
import Queue
import logging
import struct

from netdicom2 import pdu

import timer
import fsm
import dulparameters


logger = logging.getLogger(__name__)


class InvalidPrimitive(Exception):
    pass


def recv_n(sock, n):
    ret = []
    read_length = 0
    while read_length < n:
        tmp = sock.recv(n - read_length)
        ret.append(tmp)
        read_length += len(tmp)
    if read_length != n:
        raise RuntimeError('Low level Network ERROR: ')
    return ''.join(ret)


class DULServiceProvider(Thread):

    def __init__(self, socket_=None, port=None, name=''):
        """
        Three ways to call DULServiceProvider. If a port number is given,
        the DUL will wait for incoming connections on this port. If a socket
        is given, the DUL will use this socket as the client socket. If none
        is given, the DUL will not be able to accept connections (but will
        be able to initiate them.)
        """

        if socket_ and port:
            raise RuntimeError('Cannot have both socket and port')

        super(DULServiceProvider, self).__init__(name=name)

        # current primitive and pdu
        self.primitive = None
        self.pdu = None
        self.event = Queue.Queue()
        # These variables provide communication between the DUL service
        # user and the DUL service provider. An event occurs when the DUL
        # service user writes the variable self.from_service_user.
        # A primitive is sent to the service user when the DUL service provider
        # writes the variable self.to_service_user.
        # The "None" value means that nothing happens.
        self.to_service_user = Queue.Queue()
        self.from_service_user = Queue.Queue()

        # Setup the timer and finite state machines
        self.timer = timer.Timer(10)
        self.state_machine = fsm.StateMachine(self)

        if socket_:
            # A client socket has been given
            # generate an event 5
            self.event.put('Evt5')
            self.remote_client_socket = socket_
            self.remote_connection_address = None
            self.local_server_socket = None
        elif port:
            # Setup the remote server socket
            # This is the socket that will accept connections
            # from the remote DUL provider
            # start this instance of DULServiceProvider in a thread.
            self.local_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.local_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            self.local_server_port = port
            if self.local_server_port:
                try:
                    self.local_server_socket.bind((os.popen('hostname').read()[:-1], self.local_server_port))
                except IOError:
                    logger.exception("Failed to bind socket")
                self.local_server_socket.listen(1)
            else:
                self.local_server_socket = None
            self.remote_client_socket = None
            self.remote_connection_address = None
        else:
            # No port nor socket
            self.local_server_socket = None
            self.remote_client_socket = None
            self.remote_connection_address = None

        self.is_killed = False
        self.start()

    def kill(self):
        """Immediately interrupts the thread"""
        self.is_killed = True

    def stop(self):
        """Interrupts the thread if state is "Sta1" """
        if self.state_machine.current_state == 'Sta1':
            self.is_killed = True
            return True
        else:
            return False

    def send(self, params):
        self.from_service_user.put(params)

    def receive(self, wait=False, timeout=None):
        # if not self.remote_client_socket: return None
        try:
            tmp = self.to_service_user.get(wait, timeout)
            return tmp
        except Queue.Empty:
            return None

    def peek(self):
        """Look at next item to be returned by get"""
        # TODO: Fix this method
        try:
            return self.to_service_user.queue[0]
        except:
            return None

    def check_incoming_pdu(self):
        # There is something to read
        try:
            raw_pdu = self.remote_client_socket.recv(1)
        except socket.error:
            self.event.put('Evt17')
            self.remote_client_socket.close()
            self.remote_client_socket = None
            return

        if raw_pdu == '':
            # Remote port has been closed
            self.event.put('Evt17')
            self.remote_client_socket.close()
            self.remote_client_socket = None
            return
        else:
            res = recv_n(self.remote_client_socket, 1)
            raw_pdu += res
            length = recv_n(self.remote_client_socket, 4)
            raw_pdu += length
            length = struct.unpack('>L', length)
            tmp = recv_n(self.remote_client_socket, length[0])
            raw_pdu += tmp

            # Determine the type of PDU coming on remote port and set the event accordingly
            self.pdu = socket_to_pdu(raw_pdu)
            self.event.put(pdu_to_event(self.pdu))
            self.primitive = self.pdu.to_params()

    def check_timer(self):
        #logger.debug('%s: checking timer' % (self.name))
        if self.timer.check() is False:
            logger.debug('%s: timer expired' % self.name)
            self.event.put('Evt18')  # Timer expired
            return True
        else:
            return False

    def check_incoming_primitive(self):
        #logger.debug('%s: checking incoming primitive' % (self.name))
        # look at self.ReceivePrimitive for incoming primitives
        try:
            self.primitive = self.from_service_user.get(False, None)
            self.event.put(primitive_to_event(self.primitive))
            return True
        except Queue.Empty:
            return False

    def check_network(self):
        #logger.debug('%s: checking network' % (self.name))
        if self.state_machine.current_state == 'Sta13':
            # wainting for connection to close
            if self.remote_client_socket is None:
                return False
            # wait for remote connection to close
            try:
                while self.remote_client_socket.recv(1) != '':
                    continue
            except socket.error:
                return False
            # self.event.Flush() # flush event queue
            self.remote_client_socket.close()
            self.remote_client_socket = None
            self.event.put('Evt17')
            return True
        if self.local_server_socket and not self.remote_client_socket:
            # local server is listening
            a, _, _ = select.select([self.local_server_socket], [], [], 0)
            if a:
                # got an incoming connection
                self.remote_client_socket, address = self.local_server_socket.accept()
                self.event.put('Evt5')
                return True
        elif self.remote_client_socket:
            if self.state_machine.current_state == 'Sta4':
                self.event.put('Evt2')
                return True
            # check if something comes in the client socket
            a, _, _ = select.select([self.remote_client_socket], [], [], 0)
            if a:
                self.check_incoming_pdu()
                return True
        else:
            return False

    def run(self):
        logger.debug('%s: DUL loop started' % self.name)
        while not self.is_killed:
            time.sleep(0.001)
            #logger.debug('%s: starting DUL loop' % self.name)

            # catch an event
            self.check_network() or self.check_incoming_primitive() or self.check_timer()
            try:
                evt = self.event.get(False)
            except Queue.Empty:
                #logger.debug('%s: no event' % (self.name))
                continue
            self.state_machine.action(evt, self)
        logger.debug('%s: DUL loop ended' % self.name)


def primitive_to_event(primitive):
    if isinstance(primitive, dulparameters.AAssociateServiceParameters):
        if primitive.result is None:
            return 'Evt1'  # A-ASSOCIATE Request
        elif primitive.result == 0:
            return 'Evt7'  # A-ASSOCIATE Response (accept)
        else:
            return 'Evt8'  # A-ASSOCIATE Response (reject)
    elif isinstance(primitive, dulparameters.AReleaseServiceParameters):
        if primitive.result is None:
            return 'Evt11'  # A-Release Request
        else:
            return 'Evt14'  # A-Release Response
    elif isinstance(primitive, dulparameters.AAbortServiceParameters):
        return 'Evt15'
    elif isinstance(primitive, dulparameters.PDataServiceParameters):
        return 'Evt9'
    else:
        raise InvalidPrimitive


def socket_to_pdu(data):
    # Returns the PDU object associated with an incoming data stream
    pdu_type = struct.unpack('B', data[0])[0]
    if pdu_type == 0x01:
        pdu_ = pdu.AAssociateRqPDU()
        pdu_.decode(data)
    elif pdu_type == 0x02:
        pdu_ = pdu.AAssociateAcPDU()
        pdu_.decode(data)
    elif pdu_type == 0x03:
        pdu_ = pdu.AAssociateRjPDU()
        pdu_.decode(data)
    elif pdu_type == 0x04:
        pdu_ = pdu.PDataTfPDU()
        pdu_.decode(data)
    elif pdu_type == 0x05:
        pdu_ = pdu.AReleaseRqPDU()
        pdu_.decode(data)
    elif pdu_type == 0x06:
        pdu_ = pdu.AReleaseRpPDU()
        pdu_.decode(data)
    elif pdu_type == 0x07:
        pdu_ = pdu.AAbortPDU()
        pdu_.decode(data)
    else:
        logger.error('Unrecognized or invalid PDU')
        pdu_ = None
    return pdu_


def pdu_to_event(pdu_):
    if isinstance(pdu_, pdu.AAssociateRqPDU):
        return 'Evt6'
    elif isinstance(pdu_, pdu.AAssociateAcPDU):
        return 'Evt3'
    elif isinstance(pdu_, pdu.AAssociateRjPDU):
        return 'Evt4'
    elif isinstance(pdu_, pdu.PDataTfPDU):
        return 'Evt10'
    elif isinstance(pdu_, pdu.AReleaseRqPDU):
        return 'Evt12'
    elif isinstance(pdu_, pdu.AReleaseRpPDU):
        return 'Evt13'
    elif isinstance(pdu_, pdu.AAbortPDU):
        return 'Evt16'
    else:
        logger.log('Unrecognized or invalid PDU')
        return 'Evt19'