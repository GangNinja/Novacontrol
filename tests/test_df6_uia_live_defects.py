"""The three defects the first live UIA run found, pinned as regressions.

Each one passed the whole simulated-backend suite and only appeared against the
real COM transport on the disposable NovaUiaProbe window:

1. ``CurrentNativeWindowHandle`` is the ELEMENT's own handle, so comparing it
   with the bound window refused every child control inside that window.
2. ``GetCurrentPattern`` returns S_OK with a NULL pointer for an unsupported
   pattern, and a NULL comtypes pointer is not ``None`` — so every element
   reported every pattern, silently disabling the unsupported-capability
   refusal.
3. ``_pattern`` made the same ``is None`` mistake when re-acquiring a pattern.
"""

from __future__ import annotations

import ctypes

from novacontrol.desktop.uia import (
    ComtypesUiaBackend,
    UiaAdapter,
    UiaElement,
    UiaTarget,
)


def _null_pointer():
    """What comtypes returns for a NULL interface: falsy, but not ``None``."""
    return ctypes.POINTER(ctypes.c_void_p)()


class _FakeModule:
    TreeScope_Descendants = 4
    UIA_ValuePatternId = 10002
    UIA_InvokePatternId = 10000
    UIA_SelectionItemPatternId = 10010
    UIA_TogglePatternId = 10015


class _FakePatternElement:
    """An element whose patterns are supplied per pattern id."""

    def __init__(self, supported: dict[int, object]) -> None:
        self._supported = supported

    def GetCurrentPattern(self, pattern_id: int) -> object:
        return self._supported.get(pattern_id, _null_pointer())


def _backend() -> ComtypesUiaBackend:
    backend = ComtypesUiaBackend()
    backend._module = _FakeModule()  # noqa: SLF001 - transport seam under test
    return backend


def test_a_null_pattern_pointer_is_not_a_pattern() -> None:
    """A control with no patterns must report NONE, not all four."""
    backend = _backend()
    element = _FakePatternElement({})

    assert backend._patterns(element) == frozenset()  # noqa: SLF001


def test_only_a_genuinely_acquired_pattern_is_reported() -> None:
    backend = _backend()
    element = _FakePatternElement({_FakeModule.UIA_ValuePatternId: object()})

    assert backend._patterns(element) == frozenset({"ValuePattern"})  # noqa: SLF001


def test_a_null_pattern_makes_the_operation_refused() -> None:
    """Re-acquisition must raise rather than hand back a null interface."""

    class _Windowless(ComtypesUiaBackend):
        def _connect(self):  # noqa: ANN202 - fake transport
            return object()

        def _element(self, element):  # noqa: ANN001, ANN202
            return _FakePatternElement({})

    backend = _Windowless()
    backend._module = _FakeModule()  # noqa: SLF001
    described = UiaElement(
        runtime_id=(1,),
        name="Panel",
        automation_id="pnlCanvas",
        control_type="50033",
        pid=2,
        window_handle=3,
        patterns=frozenset(),
    )
    try:
        backend._pattern(described, "ValuePattern")  # noqa: SLF001
    except RuntimeError as exc:
        assert "no longer exposes ValuePattern" in str(exc)
    else:  # pragma: no cover - the bug this pins
        raise AssertionError("a null pattern pointer was accepted as a pattern")


def test_an_element_inside_the_bound_window_is_not_foreign() -> None:
    """The child's own handle differs from the window; that is not foreign."""
    target = UiaTarget(window_handle=917746, window_pid=22888, automation_id="txtMessage")
    child = UiaElement(
        runtime_id=(42, 393522),
        name="Message:",
        automation_id="txtMessage",
        control_type="50004",
        pid=22888,
        window_handle=393522,
        top_level_handle=917746,
        patterns=frozenset({"ValuePattern"}),
    )

    problem = UiaAdapter._ownership_problem(target, child)  # noqa: SLF001

    assert problem == ""


def test_an_element_in_another_window_is_foreign_even_in_the_same_process() -> None:
    target = UiaTarget(window_handle=917746, window_pid=22888, automation_id="txtMessage")
    other = UiaElement(
        runtime_id=(42, 555),
        name="Message:",
        automation_id="txtMessage",
        control_type="50004",
        pid=22888,
        window_handle=555,
        top_level_handle=555001,
        patterns=frozenset({"ValuePattern"}),
    )

    problem = UiaAdapter._ownership_problem(target, other)  # noqa: SLF001

    assert "not the bound window" in problem


def test_a_backend_that_models_whole_windows_still_checks_ownership() -> None:
    """top_level_handle 0 falls back to window_handle, as the simulator relies on."""
    target = UiaTarget(window_handle=500, window_pid=7, automation_id="ok")
    modelled = UiaElement(
        runtime_id=(1,),
        name="ok",
        automation_id="ok",
        control_type="50000",
        pid=7,
        window_handle=500,
        patterns=frozenset({"InvokePattern"}),
    )
    mismatched = UiaElement(
        runtime_id=(2,),
        name="ok",
        automation_id="ok",
        control_type="50000",
        pid=7,
        window_handle=501,
        patterns=frozenset({"InvokePattern"}),
    )

    assert UiaAdapter._ownership_problem(target, modelled) == ""  # noqa: SLF001
    assert UiaAdapter._ownership_problem(target, mismatched) != ""  # noqa: SLF001


def test_reacquisition_searches_the_top_level_window() -> None:
    """A re-find must start at the WINDOW, not the element's own child handle."""
    seen: list[int] = []

    class _Found:
        def GetRuntimeId(self):
            return (1, 2)

    class _Collection:
        Length = 1

        def GetElement(self, index: int):
            return _Found()

    class _Root:
        def FindAll(self, scope: int, condition: object):
            return _Collection()

    class _Uia:
        def ElementFromHandle(self, handle: int):
            seen.append(int(handle))
            return _Root()

        def CreateTrueCondition(self):
            return object()

    class _Windowless(ComtypesUiaBackend):
        def _connect(self):  # noqa: ANN202 - fake transport
            return _Uia()

    backend = _Windowless()
    backend._module = _FakeModule()  # noqa: SLF001
    described = UiaElement(
        runtime_id=(1, 2),
        name="",
        automation_id="txtMessage",
        control_type="50004",
        pid=1,
        window_handle=555,
        top_level_handle=999,
        patterns=frozenset({"ValuePattern"}),
    )

    found = backend._element(described)  # noqa: SLF001

    assert seen == [999]
    assert isinstance(found, _Found)
