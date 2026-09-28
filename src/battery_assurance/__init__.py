"""人形储能电池资产结构化校准数据的基础组件。"""

from .contracts import Observation, Protocol, ValidationError
from .analysis import ALGORITHM_VERSION, analyze, bootstrap_mean_interval
from .numeric import NumericSummary, WilsonInterval
from .service import TrialService

__all__ = [
    "NumericSummary",
    "Observation",
    "Protocol",
    "ValidationError",
    "WilsonInterval",
    "ALGORITHM_VERSION",
    "TrialService",
    "analyze",
    "bootstrap_mean_interval",
]

__version__ = "0.1.0"
