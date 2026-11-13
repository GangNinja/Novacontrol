"""Execution-isolation guards for NM-12.

A shadow proposal must never reach a broker, NM-10's paper engine, the wallet,
the ledger or a portfolio mutation.  This guard is the explicit barrier: it has
no gateway and every execution-shaped method raises.
"""

from __future__ import annotations

from typing import Any

from novacontrol.novamarket.shadow.models import ShadowProjection


class ShadowIsolationError(RuntimeError):
    """A shadow-mode artefact was about to enter an execution pathway."""


class ShadowExecutionGuard:
    """A deny-only execution surface.

    The methods exist so that an accidental call is a loud, typed refusal rather
    than an ImportError at runtime.  Nothing here can place, fill, reserve,
    debit, credit or reconcile.
    """

    def submit_order(self, *args: Any, **kwargs: Any) -> Any:
        raise ShadowIsolationError(
            "shadow mode cannot submit an order; proposals are hypothetical"
        )

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise ShadowIsolationError("shadow mode cannot execute a paper order")

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        raise ShadowIsolationError("shadow mode cannot place an order")

    def process_order(self, *args: Any, **kwargs: Any) -> Any:
        raise ShadowIsolationError("shadow mode cannot process a paper order")

    def reserve_capital(self, *args: Any, **kwargs: Any) -> Any:
        raise ShadowIsolationError("shadow mode cannot reserve portfolio capital")

    def debit(self, *args: Any, **kwargs: Any) -> Any:
        raise ShadowIsolationError("shadow mode cannot debit the NovaCoin ledger")

    def credit(self, *args: Any, **kwargs: Any) -> Any:
        raise ShadowIsolationError("shadow mode cannot credit the NovaCoin ledger")

    @staticmethod
    def assert_projection(projection: ShadowProjection) -> None:
        if not projection.simulated:
            raise ShadowIsolationError("a shadow projection must be simulated")
        if projection.execution_order_id:
            raise ShadowIsolationError(
                "a shadow projection must not carry an execution order id"
            )
        if projection.label != "hypothetical order projection":
            raise ShadowIsolationError(
                "a shadow projection must be labelled hypothetical"
            )
