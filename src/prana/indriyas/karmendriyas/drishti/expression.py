"""Re-export of deha.expression.expression.

Preserves the `prana.indriyas.karmendriyas.drishti.expression` import
path — the antahkarana vocabulary lives in prana, the implementation
lives in deha.
"""

from deha.expression.expression import (
    DEFAULT_DEVICE_IP,
    ExpressionClient,
)

__all__ = ["ExpressionClient", "DEFAULT_DEVICE_IP"]
