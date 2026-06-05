import hashlib
import hmac
import json
from typing import Optional
from urllib.parse import parse_qsl
from config import BOT_TOKEN

def validate_init_data(init_data: str) -> Optional:
    try:
        vals = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_check = vals.pop('hash', None)
        data_check = '\n'.join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if h!= hash_check:
            return None
        return json.loads(vals['user'])
    except Exception:
        return None
