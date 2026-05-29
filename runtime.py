
import queue
import threading
from typing import Any, Dict, Optional

price_cache: Dict[str, float] = {}
event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
state_lock = threading.Lock()
last_good_state: Optional[Dict[str, Any]] = None
scanner_cache: Dict[str, Any] = {"symbols": [], "last_refresh": 0.0}
lock_handle = None
last_discord_summary = 0.0
