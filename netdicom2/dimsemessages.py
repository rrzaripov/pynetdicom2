#
# Copyright (c) 2012 Patrice Munger
# This file is part of pynetdicom, released under a modified MIT license.
#    See the file license.txt included with this distribution, also
#    available at http://pynetdicom.googlecode.com
#

"""
    All DIMSE Message classes implement the following methods:

      from_params(DIMSEServiceParameter)    :  Builds a DIMSE message from a
                                              DULServiceParameter
                                              object. Used when receiving
                                              primitives from the
                                              DIMSEServiceUser.
      to_params()                           :  Convert the Message into a
                                              DIMSEServiceParameter object.
                                              Used for sending primitives to
                                              the DIMSEServiceUser.
      encode()                             :  Returns the encoded message in
                                              one or several P-DATA parameters
                                              structure.
      decode(pdata)                        :  Construct the message from one
                                              or several P-DATA primitives

                          from_params               encode
  |----------------------| ------->  |----------| -------> |---------------|
  | Service parameters   |           |   DIMSE  |          |     P-DATA    |
  |      object          |           |  message |          |  primitive(s) |
  |______________________| <-------  |__________| <------- |_______________|
                           to_params                decode
"""
import struct

from dicom.dataset import Dataset
from dicom.UID import ImplicitVRLittleEndian

import netdicom2.dsutils as dsutils
import netdicom2.dimseparameters
import netdicom2.exceptions as exceptions
import netdicom2.pdu as pdu

import dicom._dicom_dict as dicomdict
import dicom.datadict


#  pydicom's dictionary misses command tags. Add them.
dicomdict.DicomDictionary.update({
    0x00000000: ('UL', '1', 'Command Group Length', '', 'CommandGroupLength'),
    0x00000002: ('UI', '1', 'Affected SOP Class UID', '',
                 'AffectedSOPClassUID'),
    0x00000003: ('UI', '1', 'Requested SOP Class UID', '',
                 'RequestedSOPClassUID'),
    0x00000100: ('US', '1', 'Command Field', '', 'CommandField'),
    0x00000110: ('US', '1', 'Message ID', '', 'MessageID'),
    0x00000120: ('US', '1', 'Message ID Being Responded To', '',
                 'MessageIDBeingRespondedTo'),
    0x00000600: ('AE', '1', 'Move Destination', '', 'MoveDestination'),
    0x00000700: ('US', '1', 'Priority', '', 'Priority'),
    0x00000800: ('US', '1', 'DataSet Type', '', 'DataSetType'),
    0x00000900: ('US', '1', 'Status', '', 'Status'),
    0x00000901: ('AT', '1', 'Offending Element', '', 'OffendingElement'),
    0x00000902: ('LO', '1', 'Error Comment', '', 'ErrorComment'),
    0x00000903: ('US', '1', 'Error ID', '', 'ErrorID'),
    0x00001000: ('UI', '1', 'Affected SOP Instance UID', '',
                 'AffectedSOPInstanceUID'),
    0x00001001: ('UI', '1', 'Requested SOP Instance UID', '',
                 'RequestedSOPInstanceUID'),
    0x00001002: ('US', '1', 'Event Type ID', '', 'EventTypeID'),
    0x00001005: ('AT', '1', 'Attribute Identifier List', '',
                 'AttributeIdentifierList'),
    0x00001008: ('US', '1', 'Action Type ID', '', 'ActionTypeID'),
    0x00001020: ('US', '1', 'Number Of Remaining Sub-operations', '',
                 'NumberOfRemainingSubOperations'),
    0x00001021: ('US', '1', 'Number Of Completed Sub-operations', '',
                 'NumberOfCompletedSubOperations'),
    0x00001022: ('US', '1', 'Number Of Failed Sub-operations', '',
                 'NumberOfFailedSubOperations'),
    0x00001023: ('US', '1', 'Number Of Warning Sub-operations', '',
                 'NumberOfWarningSubOperations'),
    0x00001030: ('AE', '1', 'Move Originator Application Entity Title', '',
                 'MoveOriginatorApplicationEntityTitle'),
    0x00001031: ('US', '1', 'Move Originator Message ID', '',
                 'MoveOriginatorMessageID'),
})
dicom.datadict.keyword_dict = dict(
    [(dicom.datadict.dictionary_keyword(tag), tag)
     for tag in dicomdict.DicomDictionary])


NO_DATASET = 0x0101

PRIORITY_LOW = 0x0002
PRIORITY_MEDIUM = 0x0000
PRIORITY_HIGH = 0x0001


def value_or_none(elem):
    return elem.value if elem else None


def fragment(max_pdu_length, str_):
    s = str_
    fragments = []
    maxsize = max_pdu_length - 6
    while 1:
        fragments.append(s[:maxsize])
        s = s[maxsize:]
        if len(s) <= maxsize:
            if len(s) > 0:
                fragments.append(s)
            return fragments


def dimse_property(tag):
    """Creates property for DIMSE message using specified attribute tag

    :param tag: tuple with group and element numbers
    :return: property that gets/sets value in command dataset
    """

    def setter(self, value):
        self.command_set[tag].value = value
    return property(lambda self: value_or_none(self.command_set.get(tag)),
                    setter)


def status_mixin(dimse_class):
    """Helper decorator that defines common `status` property in provided
    DIMSE message class.

    This property is usually found in response messages.

    :param dimse_class: DIMSE message class
    :return: DIMSE message class with defined `status` property
    """
    dimse_class.status = dimse_property((0x0000, 0x0900))
    return dimse_class


def priority_mixin(dimse_class):
    """Helper decorator that defines common `priority` property in provided
    DIMSE message class.

    This property is usually found in request messages

    :param dimse_class: DIMSE message class
    :return: DIMSE message class with defined `priority` property
    """
    dimse_class.priority = dimse_property((0x0000, 0x0700))
    return dimse_class


class DIMSEMessage(object):
    command_field = None
    command_fields = []

    def __init__(self):
        self.encoded_data_set = []
        self.encoded_command_set = []
        self._data_set = ''
        self.id_ = None

        self.ts = ImplicitVRLittleEndian  # imposed by standard.

        self.command_set = Dataset()
        self.command_set.CommandField = self.command_field
        self.command_set.DataSetType = NO_DATASET
        for field in self.command_fields:
            setattr(self.command_set, field, '')

    affected_sop_class_uid = dimse_property((0x0000, 0x0002))

    @property
    def data_set(self):
        return self._data_set

    @data_set.setter
    def data_set(self, value):
        if value:
            self.command_set.DataSetType = 0x0001
        self._data_set = value

    def encode(self, id_, max_pdu_length):
        """Returns the encoded message as a series of P-DATA service
        parameter objects."""
        self.id_ = id_
        p_datas = []
        encoded_command_set = dsutils.encode(self.command_set,
                                             self.ts.is_implicit_VR,
                                             self.ts.is_little_endian)

        # fragment command set
        pdvs = fragment(max_pdu_length, encoded_command_set)
        for pdv in pdvs[:-1]:
            # send only one pdv per p-data primitive
            value_item = pdu.PresentationDataValueItem(
                self.id_, struct.pack('b', 1) + pdv)
            p_datas.append(pdu.PDataTfPDU([value_item]))

        # last command fragment
        value_item = pdu.PresentationDataValueItem(
            self.id_, struct.pack('b', 3) + pdvs[-1])
        p_datas.append(pdu.PDataTfPDU([value_item]))

        # fragment data set
        if self.data_set:
            pdvs = fragment(max_pdu_length, self.data_set)
            for pdv in pdvs[:-1]:
                value_item = pdu.PresentationDataValueItem(
                    self.id_, struct.pack('b', 0) + pdv)
                p_datas.append(pdu.PDataTfPDU([value_item]))
            # last data fragment
            value_item = pdu.PresentationDataValueItem(
                self.id_, struct.pack('b', 2) + pdvs[-1])
            p_datas.append(pdu.PDataTfPDU([value_item]))

        return p_datas

    def decode(self, p_data):
        """Constructs itself receiving a series of P-DATA primitives.
        Returns True when complete, False otherwise."""
        if not isinstance(p_data, pdu.PDataTfPDU):
            return False

        for value_item in p_data.data_value_items:
            # must be able to read P-DATA with several PDVs
            self.id_ = value_item.context_id
            marker = struct.unpack('b', value_item.data_value[0])[0]
            if marker in (1, 3):
                self.encoded_command_set.append(value_item.data_value[1:])
                if marker == 3:
                    self.command_set = dsutils.decode(
                        ''.join(self.encoded_command_set),
                        self.ts.is_implicit_VR, self.ts.is_little_endian)
                    self.encoded_command_set = []
                    self.__class__ = MESSAGE_TYPE[
                        self.command_set[(0x0000, 0x0100)].value]
                    if self.command_set[(0x0000, 0x0800)].value == 0x0101:
                        return True  # response: no dataset
            elif marker in (0, 2):
                self.encoded_data_set.append(value_item.data_value[1:])
                if marker == 2:
                    self.data_set = ''.join(self.encoded_data_set)
                    self.encoded_data_set = []
                    return True
            else:
                raise exceptions.DIMSEProcessingError(
                    'Incorrect first PDV byte')

        return False

    def set_length(self):
        it = (len(dsutils.encode_element(v, self.ts.is_implicit_VR,
                                         self.ts.is_little_endian))
              for v in self.command_set.values()[1:])
        self.command_set[(0x0000, 0x0000)].value = sum(it)

    def __repr__(self):
        return str(self.command_set) + '\n'


class DIMSERequestMessage(DIMSEMessage):
    message_id = dimse_property((0x0000, 0x0110))


class DIMSEResponseMessage(DIMSEMessage):
    message_id_being_responded_to = dimse_property((0x0000, 0x0120))


class CEchoRQMessage(DIMSERequestMessage):
    command_field = 0x0030
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID', 'MessageID']

    def from_params(self, params):
        self.command_set[(0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[(0x0000, 0x0110)].value = params.message_id
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CEchoServiceParameters()
        tmp.message_id = self.command_set.get((0x0000, 0x0110))
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        return tmp


@status_mixin
class CEchoRSPMessage(DIMSEResponseMessage):
    command_field = 0x8030
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID',
                      'MessageIDBeingRespondedTo', 'Status']

    def from_params(self, params):
        if params.affected_sop_class_uid:
            self.command_set[
                (0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[
            (0x0000, 0x0120)].value = params.message_id_being_responded_to
        self.command_set[(0x0000, 0x0900)].value = params.status
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CEchoServiceParameters()
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        tmp.message_id_being_responded_to = self.command_set.get(
            (0x0000, 0x0120))
        tmp.status = 0
        return tmp


@priority_mixin
class CStoreRQMessage(DIMSERequestMessage):
    command_field = 0x0001
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID',
                      'MessageID', 'Priority', 'AffectedSOPInstanceUID',
                      'MoveOriginatorApplicationEntityTitle',
                      'MoveOriginatorMessageID']
    affected_sop_instance_uid = dimse_property((0x0000, 0x1000))
    move_originator_aet = dimse_property((0x0000, 0x1030))
    move_originator_message_id = dimse_property((0x0000, 0x1031))

    def from_params(self, params):
        self.command_set[(0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[(0x0000, 0x0110)].value = params.message_id
        self.command_set[(0x0000, 0x0700)].value = params.priority
        self.command_set[
            (0x0000, 0x1000)].value = params.affected_sop_instance_uid
        if params.move_originator_aet:
            self.command_set[(0x0000,
                              0x1030)].value = params.move_originator_aet
        else:
            self.command_set[(0x0000, 0x1030)].value = ''
        if params.move_originator_message_id:
            self.command_set[
                (0x0000, 0x1031)].value = params.move_originator_message_id
        else:
            self.command_set[(0x0000, 0x1031)].value = ''
        self.data_set = params.data_set
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CStoreServiceParameters()
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        tmp.affected_sop_instance_uid = self.command_set.get((0x0000, 0x1000))
        tmp.priority = self.command_set.get((0x0000, 0x0700))
        tmp.dataset = self.data_set
        tmp.message_id = self.command_set.get((0x0000, 0x0110))
        return tmp


@status_mixin
class CStoreRSPMessage(DIMSEResponseMessage):
    command_field = 0x0101
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID',
                      'MessageIDBeingRespondedTo', 'Status',
                      'AffectedSOPInstanceUID']
    affected_sop_instance_uid = dimse_property((0x0000, 0x1000))

    def from_params(self, params):
        self.command_set[
            (0x0000, 0x0002)].value = params.affected_sop_class_uid.value
        self.command_set[
            (0x0000, 0x0120)].value = params.message_id_being_responded_to.value
        self.command_set[(0x0000, 0x0900)].value = params.status
        self.command_set[
            (0x0000, 0x1000)].value = params.affected_sop_instance_uid.value
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CStoreServiceParameters()
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        tmp.message_id_being_responded_to = self.command_set.get(
            (0x0000, 0x0120))
        tmp.status = self.command_set.get((0x0000, 0x0900))
        tmp.affected_sop_instance_uid = self.command_set.get((0x0000, 0x1000))
        tmp.dataset = self.data_set
        return tmp


@priority_mixin
class CFindRQMessage(DIMSERequestMessage):
    command_field = 0x0020
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID', 'MessageID',
                      'Priority']

    def from_params(self, params):
        self.command_set[(0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[(0x0000, 0x0110)].value = params.message_id
        self.command_set[(0x0000, 0x0700)].value = params.priority
        self.data_set = params.identifier
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CFindServiceParameters()
        tmp.affected_sop_class_uid = self.command_set[(0x0000, 0x0002)]
        tmp.priority = self.command_set.get((0x0000, 0x0700))
        tmp.identifier = self.data_set
        tmp.message_id = self.command_set.get((0x0000, 0x0110))
        return tmp


@status_mixin
class CFindRSPMessage(DIMSEResponseMessage):
    command_field = 0x0101
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID',
                      'MessageIDBeingRespondedTo', 'Status']

    def from_params(self, params):
        self.command_set[
            (0x0000, 0x0002)].value = params.affected_sop_class_uid.value
        self.command_set[
            (0x0000, 0x0120)].value = params.message_id_being_responded_to.value
        self.command_set[(0x0000, 0x0900)].value = params.status
        self.data_set = params.identifier
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CFindServiceParameters()
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        tmp.message_id_being_responded_to = self.command_set.get(
            (0x0000, 0x0120))
        tmp.status = self.command_set.get((0x0000, 0x0900))
        tmp.identifier = self.data_set
        return tmp


@priority_mixin
class CGetRQMessage(DIMSERequestMessage):
    command_field = 0x0010
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID', 'MessageID',
                      'Priority']

    def from_params(self, params):
        self.command_set[(0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[(0x0000, 0x0110)].value = params.message_id
        self.command_set[(0x0000, 0x0700)].value = params.priority
        self.data_set = params.identifier
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CGetServiceParameters()
        tmp.message_id = self.command_set.get((0x0000, 0x0110)).value
        tmp.affected_sop_class_uid = self.command_set.get(
            (0x0000, 0x0002)).value
        tmp.priority = self.command_set.get((0x0000, 0x0700)).value
        tmp.identifier = self.data_set
        return tmp


@status_mixin
class CGetRSPMessage(DIMSEResponseMessage):
    command_field = 0x8010
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID',
                      'MessageIDBeingRespondedTo', 'Status',
                      'NumberOfRemainingSubOperations',
                      'NumberOfCompletedSubOperations',
                      'NumberOfFailedSubOperations',
                      'NumberOfWarningSubOperations']
    num_of_remaining_sub_ops = dimse_property((0x0000, 0x1020))
    num_of_completed_sub_ops = dimse_property((0x0000, 0x1021))
    num_of_failed_sub_ops = dimse_property((0x0000, 0x1022))
    num_of_warning_sub_ops = dimse_property((0x0000, 0x1023))

    def from_params(self, params):
        self.command_set[(0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[
            (0x0000, 0x0120)].value = params.message_id_being_responded_to
        self.command_set[(0x0000, 0x0900)].value = params.status
        self.command_set[
            (0x0000, 0x1020)].value = params.num_of_remaining_sub_ops
        self.command_set[
            (0x0000, 0x1021)].value = params.num_of_completed_sub_ops
        self.command_set[(0x0000, 0x1022)].value = params.num_of_failed_sub_ops
        self.command_set[(0x0000, 0x1023)].value = params.num_of_warning_sub_ops
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CGetServiceParameters()
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        tmp.message_id_being_responded_to = self.command_set.get(
            (0x0000, 0x0120))
        tmp.status = self.command_set.get((0x0000, 0x0900))
        tmp.num_of_remaining_sub_ops = self.command_set.get((0x0000, 0x1020))
        tmp.num_of_completed_sub_ops = self.command_set.get((0x0000, 0x1021))
        tmp.num_of_failed_sub_ops = self.command_set.get((0x0000, 0x1022))
        tmp.num_of_warning_sub_ops = self.command_set.get((0x0000, 0x1023))
        tmp.identifier = self.data_set
        return tmp


@priority_mixin
class CMoveRQMessage(DIMSERequestMessage):
    command_field = 0x0021
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID',
                      'MessageID', 'Priority', 'MoveDestination']
    move_destination = dimse_property((0x0000, 0x0700))

    def from_params(self, params):
        self.command_set[(0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[(0x0000, 0x0110)].value = params.message_id
        self.command_set[(0x0000, 0x0700)].value = params.priority
        self.command_set[(0x0000, 0x0600)].value = params.move_destination

        self.data_set = params.identifier
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CMoveServiceParameters()
        tmp.message_id = self.command_set.get((0x0000, 0x0110))
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        tmp.priority = self.command_set.get((0x0000, 0x0700))
        tmp.move_destination = self.command_set.get((0x0000, 0x0600))
        tmp.identifier = self.data_set
        return tmp


@status_mixin
class CMoveRSPMessage(DIMSEResponseMessage):
    command_field = 0x8021
    command_fields = ['CommandGroupLength', 'AffectedSOPClassUID',
                      'MessageIDBeingRespondedTo', 'Status',
                      'NumberOfRemainingSubOperations',
                      'NumberOfCompletedSubOperations',
                      'NumberOfFailedSubOperations',
                      'NumberOfWarningSubOperations']
    num_of_remaining_sub_ops = dimse_property((0x0000, 0x1020))
    num_of_completed_sub_ops = dimse_property((0x0000, 0x1021))
    num_of_failed_sub_ops = dimse_property((0x0000, 0x1022))
    num_of_warning_sub_ops = dimse_property((0x0000, 0x1023))

    def from_params(self, params):
        self.command_set[(0x0000, 0x0002)].value = params.affected_sop_class_uid
        self.command_set[
            (0x0000, 0x0120)].value = params.message_id_being_responded_to
        self.command_set[(0x0000, 0x0900)].value = params.status
        self.command_set[
            (0x0000, 0x1020)].value = params.num_of_remaining_sub_ops
        self.command_set[
            (0x0000, 0x1021)].value = params.num_of_completed_sub_ops
        self.command_set[(0x0000, 0x1022)].value = params.num_of_failed_sub_ops
        self.command_set[(0x0000, 0x1023)].value = params.num_of_warning_sub_ops
        self.set_length()

    def to_params(self):
        tmp = netdicom2.dimseparameters.CMoveServiceParameters()
        tmp.affected_sop_class_uid = self.command_set.get((0x0000, 0x0002))
        tmp.message_id_being_responded_to = self.command_set.get(
            (0x0000, 0x0120))
        tmp.status = self.command_set.get((0x0000, 0x0900))
        tmp.num_of_remaining_sub_ops = self.command_set.get((0x0000, 0x1020))
        tmp.num_of_completed_sub_ops = self.command_set.get((0x0000, 0x1021))
        tmp.num_of_failed_sub_ops = self.command_set.get((0x0000, 0x1022))
        tmp.num_of_warning_sub_ops = self.command_set.get((0x0000, 0x1023))
        tmp.identifier = self.data_set
        return tmp


class CCancelRQMessage(DIMSEResponseMessage):
    command_field = 0x0FFF
    command_fields = ['CommandGroupLength', 'MessageIDBeingRespondedTo']

    def from_params(self, params):
        self.command_set[
            (0x0000, 0x0120)].value = params.message_id_being_responded_to
        self.set_length()


class CCancelFindRQMessage(CCancelRQMessage):
    def to_params(self):
        tmp = netdicom2.dimseparameters.CFindServiceParameters()
        tmp.message_id_being_responded_to = self.command_set[(0x0000, 0x0120)]
        return tmp


class CCancelGetRQMessage(CCancelRQMessage):
    def to_params(self):
        tmp = netdicom2.dimseparameters.CGetServiceParameters()
        tmp.message_id_being_responded_to = self.command_set[(0x0000, 0x0120)]
        return tmp


class CCancelMoveRQMessage(CCancelRQMessage):
    def to_params(self):
        tmp = netdicom2.dimseparameters.CMoveServiceParameters()
        tmp.message_id_being_responded_to = self.command_set[(0x0000, 0x0120)]
        return tmp


MESSAGE_TYPE = {
    0x0001: CStoreRQMessage,
    0x8001: CStoreRSPMessage,
    0x0020: CFindRQMessage,
    0x8020: CFindRSPMessage,
    0x0FFF: CCancelRQMessage,
    0x0010: CGetRQMessage,
    0x8010: CGetRSPMessage,
    0x0021: CMoveRQMessage,
    0x8021: CMoveRSPMessage,
    0x0030: CEchoRQMessage,
    0x8030: CEchoRSPMessage
}
