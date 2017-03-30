from __future__ import print_function, absolute_import, unicode_literals
import re
import six
from zope.interface import implementer
from attr import attrs, attrib
from attr.validators import provides, instance_of
from twisted.python import log
from automat import MethodicalMachine
from . import _interfaces
from ._nameplate import Nameplate
from ._mailbox import Mailbox
from ._send import Send
from ._order import Order
from ._key import Key
from ._receive import Receive
from ._rendezvous import RendezvousConnector
from ._lister import Lister
from ._allocator import Allocator
from ._input import Input
from ._code import Code
from ._terminator import Terminator
from ._wordlist import PGPWordList
from .errors import (ServerError, LonelyError, WrongPasswordError,
                     KeyFormatError, OnlyOneCodeError, _UnknownPhaseError)
from .util import bytes_to_dict

@attrs
@implementer(_interfaces.IBoss)
class Boss(object):
    _W = attrib()
    _side = attrib(validator=instance_of(type(u"")))
    _url = attrib(validator=instance_of(type(u"")))
    _appid = attrib(validator=instance_of(type(u"")))
    _versions = attrib(validator=instance_of(dict))
    _welcome_handler = attrib() # TODO: validator: callable
    _reactor = attrib()
    _journal = attrib(validator=provides(_interfaces.IJournal))
    _tor_manager = attrib() # TODO: ITorManager or None
    _timing = attrib(validator=provides(_interfaces.ITiming))
    m = MethodicalMachine()
    set_trace = m.setTrace

    def __attrs_post_init__(self):
        self._build_workers()
        self._init_other_state()

    def _build_workers(self):
        self._N = Nameplate()
        self._M = Mailbox(self._side)
        self._S = Send(self._side, self._timing)
        self._O = Order(self._side, self._timing)
        self._K = Key(self._appid, self._versions, self._side, self._timing)
        self._R = Receive(self._side, self._timing)
        self._RC = RendezvousConnector(self._url, self._appid, self._side,
                                       self._reactor, self._journal,
                                       self._tor_manager, self._timing)
        self._L = Lister(self._timing)
        self._A = Allocator(self._timing)
        self._I = Input(self._timing)
        self._C = Code(self._timing)
        self._T = Terminator()

        self._N.wire(self._M, self._I, self._RC, self._T)
        self._M.wire(self._N, self._RC, self._O, self._T)
        self._S.wire(self._M)
        self._O.wire(self._K, self._R)
        self._K.wire(self, self._M, self._R)
        self._R.wire(self, self._S)
        self._RC.wire(self, self._N, self._M, self._A, self._L, self._T)
        self._L.wire(self._RC, self._I)
        self._A.wire(self._RC, self._C)
        self._I.wire(self._C, self._L)
        self._C.wire(self, self._A, self._N, self._K, self._I)
        self._T.wire(self, self._RC, self._N, self._M)

    def _init_other_state(self):
        self._did_start_code = False
        self._next_tx_phase = 0
        self._next_rx_phase = 0
        self._rx_phases = {} # phase -> plaintext

        self._result = "empty"

    # these methods are called from outside
    def start(self):
        self._RC.start()

    def _set_trace(self, client_name, which, logger):
        names = {"B": self, "N": self._N, "M": self._M, "S": self._S,
                 "O": self._O, "K": self._K, "SK": self._K._SK, "R": self._R,
                 "RC": self._RC, "L": self._L, "C": self._C,
                 "T": self._T}
        for machine in which.split():
            def tracer(old_state, input, new_state, output, machine=machine):
                if output is None:
                    if new_state:
                        print("%s.%s[%s].%s -> [%s]" %
                              (client_name, machine, old_state, input,
                               new_state))
                    else:
                        # the RendezvousConnector emits message events as if
                        # they were state transitions, except that old_state
                        # and new_state are empty strings. "input" is one of
                        # R.connected, R.rx(type phase+side), R.tx(type
                        # phase), R.lost .
                        print("%s.%s.%s" % (client_name, machine, input))
                else:
                    if new_state:
                        print(" %s.%s.%s()" % (client_name, machine, output))
            names[machine].set_trace(tracer)

    def serialize(self):
        raise NotImplemented

    # and these are the state-machine transition functions, which don't take
    # args
    @m.state(initial=True)
    def S0_empty(self): pass # pragma: no cover
    @m.state()
    def S1_lonely(self): pass # pragma: no cover
    @m.state()
    def S2_happy(self): pass # pragma: no cover
    @m.state()
    def S3_closing(self): pass # pragma: no cover
    @m.state(terminal=True)
    def S4_closed(self): pass # pragma: no cover

    # from the Wormhole

    # input/allocate/set_code are regular methods, not state-transition
    # inputs. We expect them to be called just after initialization, while
    # we're in the S0_empty state. You must call exactly one of them, and the
    # call must happen while we're in S0_empty, which makes them good
    # candiates for being a proper @m.input, but set_code() will immediately
    # (reentrantly) cause self.got_code() to be fired, which is messy. These
    # are all passthroughs to the Code machine, so one alternative would be
    # to have Wormhole call Code.{input,allocate,set_code} instead, but that
    # would require the Wormhole to be aware of Code (whereas right now
    # Wormhole only knows about this Boss instance, and everything else is
    # hidden away).
    def input_code(self):
        if self._did_start_code:
            raise OnlyOneCodeError()
        self._did_start_code = True
        return self._C.input_code()
    def allocate_code(self, code_length):
        if self._did_start_code:
            raise OnlyOneCodeError()
        self._did_start_code = True
        wl = PGPWordList()
        self._C.allocate_code(code_length, wl)
    def set_code(self, code):
        if ' ' in code:
            raise KeyFormatError("code (%s) contains spaces." % code)
        if self._did_start_code:
            raise OnlyOneCodeError()
        self._did_start_code = True
        self._C.set_code(code)

    @m.input()
    def send(self, plaintext): pass
    @m.input()
    def close(self): pass

    # from RendezvousConnector. rx_error an error message from the server
    # (probably because of something we did, or due to CrowdedError). error
    # is when an exception happened while it tried to deliver something else
    @m.input()
    def rx_welcome(self, welcome): pass
    @m.input()
    def rx_error(self, errmsg, orig): pass
    @m.input()
    def error(self, err): pass

    # from Code (provoked by input/allocate/set_code)
    @m.input()
    def got_code(self, code): pass

    # Key sends (got_key, got_verifier, scared)
    # Receive sends (got_message, happy, scared)
    @m.input()
    def happy(self): pass
    @m.input()
    def scared(self): pass

    def got_message(self, phase, plaintext):
        assert isinstance(phase, type("")), type(phase)
        assert isinstance(plaintext, type(b"")), type(plaintext)
        if phase == "version":
            self._got_version(plaintext)
        elif re.search(r'^\d+$', phase):
            self._got_phase(int(phase), plaintext)
        else:
            # Ignore unrecognized phases, for forwards-compatibility. Use
            # log.err so tests will catch surprises.
            log.err(_UnknownPhaseError("received unknown phase '%s'" % phase))
    @m.input()
    def _got_version(self, plaintext): pass
    @m.input()
    def _got_phase(self, phase, plaintext): pass
    @m.input()
    def got_key(self, key): pass
    @m.input()
    def got_verifier(self, verifier): pass

    # Terminator sends closed
    @m.input()
    def closed(self): pass


    @m.output()
    def process_welcome(self, welcome):
        self._welcome_handler(welcome)

    @m.output()
    def do_got_code(self, code):
        self._W.got_code(code)
    @m.output()
    def process_version(self, plaintext):
        # most of this is wormhole-to-wormhole, ignored for now
        # in the future, this is how Dilation is signalled
        self._their_versions = bytes_to_dict(plaintext)
        # but this part is app-to-app
        app_versions = self._their_versions.get("app_versions", {})
        self._W.got_version(app_versions)

    @m.output()
    def S_send(self, plaintext):
        assert isinstance(plaintext, type(b"")), type(plaintext)
        phase = self._next_tx_phase
        self._next_tx_phase += 1
        self._S.send("%d" % phase, plaintext)

    @m.output()
    def close_error(self, errmsg, orig):
        self._result = ServerError(errmsg)
        self._T.close("errory")
    @m.output()
    def close_scared(self):
        self._result = WrongPasswordError()
        self._T.close("scary")
    @m.output()
    def close_lonely(self):
        self._result = LonelyError()
        self._T.close("lonely")
    @m.output()
    def close_happy(self):
        self._result = "happy"
        self._T.close("happy")

    @m.output()
    def W_got_key(self, key):
        self._W.got_key(key)
    @m.output()
    def W_got_verifier(self, verifier):
        self._W.got_verifier(verifier)
    @m.output()
    def W_received(self, phase, plaintext):
        assert isinstance(phase, six.integer_types), type(phase)
        # we call Wormhole.received() in strict phase order, with no gaps
        self._rx_phases[phase] = plaintext
        while self._next_rx_phase in self._rx_phases:
            self._W.received(self._rx_phases.pop(self._next_rx_phase))
            self._next_rx_phase += 1

    @m.output()
    def W_close_with_error(self, err):
        self._result = err # exception
        self._W.closed(self._result)

    @m.output()
    def W_closed(self):
        # result is either "happy" or a WormholeError of some sort
        self._W.closed(self._result)

    S0_empty.upon(close, enter=S3_closing, outputs=[close_lonely])
    S0_empty.upon(send, enter=S0_empty, outputs=[S_send])
    S0_empty.upon(rx_welcome, enter=S0_empty, outputs=[process_welcome])
    S0_empty.upon(got_code, enter=S1_lonely, outputs=[do_got_code])
    S0_empty.upon(rx_error, enter=S3_closing, outputs=[close_error])
    S0_empty.upon(error, enter=S4_closed, outputs=[W_close_with_error])

    S1_lonely.upon(rx_welcome, enter=S1_lonely, outputs=[process_welcome])
    S1_lonely.upon(happy, enter=S2_happy, outputs=[])
    S1_lonely.upon(scared, enter=S3_closing, outputs=[close_scared])
    S1_lonely.upon(close, enter=S3_closing, outputs=[close_lonely])
    S1_lonely.upon(send, enter=S1_lonely, outputs=[S_send])
    S1_lonely.upon(got_key, enter=S1_lonely, outputs=[W_got_key])
    S1_lonely.upon(got_verifier, enter=S1_lonely, outputs=[W_got_verifier])
    S1_lonely.upon(rx_error, enter=S3_closing, outputs=[close_error])
    S1_lonely.upon(error, enter=S4_closed, outputs=[W_close_with_error])

    S2_happy.upon(rx_welcome, enter=S2_happy, outputs=[process_welcome])
    S2_happy.upon(_got_phase, enter=S2_happy, outputs=[W_received])
    S2_happy.upon(_got_version, enter=S2_happy, outputs=[process_version])
    S2_happy.upon(scared, enter=S3_closing, outputs=[close_scared])
    S2_happy.upon(close, enter=S3_closing, outputs=[close_happy])
    S2_happy.upon(send, enter=S2_happy, outputs=[S_send])
    S2_happy.upon(rx_error, enter=S3_closing, outputs=[close_error])
    S2_happy.upon(error, enter=S4_closed, outputs=[W_close_with_error])

    S3_closing.upon(rx_welcome, enter=S3_closing, outputs=[])
    S3_closing.upon(rx_error, enter=S3_closing, outputs=[])
    S3_closing.upon(_got_phase, enter=S3_closing, outputs=[])
    S3_closing.upon(_got_version, enter=S3_closing, outputs=[])
    S3_closing.upon(happy, enter=S3_closing, outputs=[])
    S3_closing.upon(scared, enter=S3_closing, outputs=[])
    S3_closing.upon(close, enter=S3_closing, outputs=[])
    S3_closing.upon(send, enter=S3_closing, outputs=[])
    S3_closing.upon(closed, enter=S4_closed, outputs=[W_closed])
    S3_closing.upon(error, enter=S4_closed, outputs=[W_close_with_error])

    S4_closed.upon(rx_welcome, enter=S4_closed, outputs=[])
    S4_closed.upon(_got_phase, enter=S4_closed, outputs=[])
    S4_closed.upon(_got_version, enter=S4_closed, outputs=[])
    S4_closed.upon(happy, enter=S4_closed, outputs=[])
    S4_closed.upon(scared, enter=S4_closed, outputs=[])
    S4_closed.upon(close, enter=S4_closed, outputs=[])
    S4_closed.upon(send, enter=S4_closed, outputs=[])
    S4_closed.upon(error, enter=S4_closed, outputs=[])
