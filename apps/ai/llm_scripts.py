"""Server-side (Lua) versions of the guard's Redis work: one round trip, still atomic.

Without them a guarded model call cost about thirty sequential Redis commands (kill switch,
pause, breaker, four request counters, three token reservations, settlement, metrics, the
rolling window): at a typical round-trip time that was seconds of added latency. Here
``acquire`` (before the call) and ``finish`` (after it) are ONE command each, and the
counters are updated and rolled back inside Redis, so atomicity is kept (and improved).

Used only when the shared cache is Redis; with the in-memory cache (tests, local runs
without ``REDIS_URL``) the guard keeps its plain per-command code, which behaves the same
(``test_llm_guard_scripts.py`` runs the same scenarios against both). Key names, TTLs and
reasons are identical, so the two paths can even be mixed. Nothing here touches customer
text: keys hold counters and opaque customer IDs only.
"""

from __future__ import annotations

from typing import Any

_BUMP = """
local function bump(key, delta, ttl)
  local v = redis.call('INCRBY', key, delta)
  if v == delta then redis.call('EXPIRE', key, ttl) end
  return v
end
"""

# KEYS: 1 kill, 2 pause, 3 breaker-open, 4 breaker-fails, 5 breaker-trial, 6 customer/minute,
#       7 customer/day, 8 global/minute, 9 global/day, 10 customer tokens, 11 minute tokens,
#       12 day tokens
# ARGV: 1 breaker threshold, 2 trial ttl, 3 per-customer/minute cap (0 = none), 4 customer/day
#       cap (0 = none), 5 global/minute cap (0 = none), 6 global/day cap, 7 token estimate
#       (0 = no token budget), 8-10 customer/minute/day token caps, 11 minute ttl, 12 day ttl
ACQUIRE = (
    _BUMP
    + """
local ttl_min, ttl_day = tonumber(ARGV[11]), tonumber(ARGV[12])
if redis.call('EXISTS', KEYS[1]) == 1 then return 'kill_switch' end
if redis.call('EXISTS', KEYS[2]) == 1 then return 'paused' end
if redis.call('EXISTS', KEYS[3]) == 1 then return 'circuit_open' end
local trial = false
local fails = tonumber(redis.call('GET', KEYS[4]) or 0) or 0
if fails >= tonumber(ARGV[1]) then
  if not redis.call('SET', KEYS[5], 1, 'NX', 'EX', tonumber(ARGV[2])) then
    return 'circuit_open'
  end
  trial = true
end
local est = tonumber(ARGV[7])
local reserved = {}
local function deny(reason)
  if trial then redis.call('DEL', KEYS[5]) end
  for i = 1, #reserved do redis.call('INCRBY', reserved[i], -est) end
  return reason
end
local per_customer = tonumber(ARGV[3])
local customer_daily = tonumber(ARGV[4])
local minute_cap = tonumber(ARGV[5])
if per_customer > 0 and bump(KEYS[6], 1, ttl_min) > per_customer then
  return deny('customer_limit')
end
if customer_daily > 0 and bump(KEYS[7], 1, ttl_day) > customer_daily then
  return deny('customer_daily')
end
if minute_cap > 0 and bump(KEYS[8], 1, ttl_min) > minute_cap then
  return deny('minute_cap')
end
if bump(KEYS[9], 1, ttl_day) > tonumber(ARGV[6]) then return deny('daily_cap') end
if est > 0 then
  local plan = {
    {10, 8, ttl_day, 'customer_budget'},
    {11, 9, ttl_min, 'minute_token_cap'},
    {12, 10, ttl_day, 'token_cap'},
  }
  for _, p in ipairs(plan) do
    local v = bump(KEYS[p[1]], est, p[3])
    reserved[#reserved + 1] = KEYS[p[1]]
    if v > tonumber(ARGV[p[2]]) then return deny(p[4]) end
  end
end
return 'ok'
"""
)

# KEYS: 1-3 token counters (customer, minute, day), 4 stat calls, 5 stat tokens, 6 breaker
#       fails, 7 breaker open, 8 breaker trial, 9-11 this minute's window calls/failures/
#       rejects, 12 kill switch, then W call buckets, W failure buckets, W reject buckets
# ARGV: 1 token delta (actual - estimate), 2 actual tokens, 3 success, 4 failed, 5 rejected,
#       6 breaker threshold, 7 breaker cool-down, 8 day ttl, 9 stat ttl, 10 window ttl,
#       11 window minutes W, 12 trip minimum calls, 13 trip failure rate, 14 trip reject
#       rate, 15 trip cool-off, 16 settle tokens (1/0), 17 minute ttl
# Returns {tripped, calls, failures, rejects, breaker-opened-at-n (0 = no)}
FINISH = (
    _BUMP
    + """
local delta, actual = tonumber(ARGV[1]), tonumber(ARGV[2])
local ttl_day, stat_ttl, win_ttl = tonumber(ARGV[8]), tonumber(ARGV[9]), tonumber(ARGV[10])
if delta ~= 0 and ARGV[16] == '1' then
  bump(KEYS[1], delta, ttl_day)
  bump(KEYS[2], delta, tonumber(ARGV[17]))
  bump(KEYS[3], delta, ttl_day)
end
bump(KEYS[4], 1, stat_ttl)
if actual > 0 then bump(KEYS[5], actual, stat_ttl) end
local opened = 0
if ARGV[3] == '1' then
  redis.call('DEL', KEYS[6], KEYS[7], KEYS[8])
else
  local n = bump(KEYS[6], 1, 3600)
  redis.call('DEL', KEYS[8])
  if n >= tonumber(ARGV[6]) then
    if redis.call('SET', KEYS[7], 1, 'NX', 'EX', tonumber(ARGV[7])) then opened = n end
  end
end
bump(KEYS[9], 1, win_ttl)
if ARGV[4] == '1' then bump(KEYS[10], 1, win_ttl) end
if ARGV[5] == '1' then bump(KEYS[11], 1, win_ttl) end
local W = tonumber(ARGV[11])
local calls, fails, rejs = 0, 0, 0
for i = 1, W do
  calls = calls + (tonumber(redis.call('GET', KEYS[12 + i])) or 0)
  fails = fails + (tonumber(redis.call('GET', KEYS[12 + W + i])) or 0)
  rejs = rejs + (tonumber(redis.call('GET', KEYS[12 + 2 * W + i])) or 0)
end
local tripped = 0
if calls >= tonumber(ARGV[12]) and
   (fails / calls > tonumber(ARGV[13]) or rejs / calls > tonumber(ARGV[14])) then
  if redis.call('SET', KEYS[12], 2, 'NX', 'EX', tonumber(ARGV[15])) then
    tripped = 1
    for i = 1, 3 * W do redis.call('DEL', KEYS[12 + i]) end
  end
end
return {tripped, calls, fails, rejs, opened}
"""
)

BUMP = (
    _BUMP
    + """
return bump(KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]))
"""
)


def connection(cache: Any) -> tuple[Any, Any] | None:
    """``(redis_client, make_key)`` when ``cache`` is Django's Redis cache, else ``None``."""
    inner = getattr(cache, "_cache", None)
    get_client = getattr(inner, "get_client", None)
    make_key = getattr(cache, "make_key", None)
    if get_client is None or make_key is None:
        return None
    return get_client(write=True), make_key


def run(client: Any, script: str, keys: list[str], args: list[Any]) -> Any:
    return client.eval(script, len(keys), *keys, *args)


def as_text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
