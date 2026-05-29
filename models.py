
from dataclasses import dataclass
from typing import Optional


@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: float
    cost_basis: float
    opened_at: str
    highest_price: float
    trailing_active: bool
    trailing_stop_price: Optional[float]
    break_even_active: bool
    entry_score: int
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
