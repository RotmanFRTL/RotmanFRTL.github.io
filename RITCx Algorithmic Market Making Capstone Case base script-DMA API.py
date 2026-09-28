"""
RIT Market Simulator Algorithmic Market Making Capstone Case - Support File
Rotman BMO Finance Research and Trading Lab, University of Toronto (C)
All rights reserved.
"""

import signal
import requests
from time import sleep
import base64

'''
If you are not familiar with Python or feeling a little bit rusty, we highly recommend going through:
    https://github.com/trekhleb/learn-python

If you have any question about DMA APIs and outputs of code please read:
    https://realpython.com/api-integration-in-python/#http-methods
    https://rit.306w.ca/RIT-DMA-API/1.0.5/

So basically:
This is a starting point, not a finished strategy. For each of CNR, RY and AC in turn, it joins
the best bid and the best ask, gives those orders a moment to fill, then cancels whatever is left.
It quotes all three securities the same way even though their fees and rebates differ, and it does
nothing to rebalance inventory beyond staying inside the position limits. Improving on both is the
heart of the case.
'''

# ============================================================================
# DMA REST API CONNECTION BLOCK
# ----------------------------------------------------------------------------
# The DMA (Direct Market Access) REST API talks to the RIT *Server* directly,
# so you do NOT need the RIT Client running on your machine. Compared to the
# Client REST API only two things change:
#
#   1. AUTH. Basic authentication with your TraderID and password, instead of
#      the {'X-API-Key': 'Rotman'} header.
#   2. RATE LIMITING. The RIT Client used to absorb this for you. On DMA there
#      is nothing in between you and the server, so a burst of requests comes
#      back as HTTP 429 and you must back off yourself.
#
# Every endpoint, parameter and JSON response is otherwise IDENTICAL to the
# Client REST API.
# ============================================================================

API_ENDPOINT = "http://flserver.rotman.utoronto.ca:PORT/v1"    # Market Making case: replace PORT with your event's port
USERNAME = "YOUR TRADER ID"
PASSWORD = "YOUR PASSWORD"
AUTHORIZATION = {'Authorization': 'Basic ' + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()}

shutdown = False


# this class definition allows us to print error messages and stop the program when needed
class ApiException(Exception):
    pass


# this signal handler allows for a graceful shutdown when CTRL+C is pressed
def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True


# this helper method handles rate-limiting to pause for the next cycle
def handle_rate_limit(response):
    if response.status_code == 429:
        # use the server's Retry-After header, else the "wait" in the body, else 1 second
        try:
            wait_time = float(response.headers.get('Retry-After') or response.json()['wait'])
        except (ValueError, KeyError, TypeError):
            wait_time = 1.0
        print(f"Rate limit exceeded. Waiting for {wait_time} seconds before retrying.")
        sleep(wait_time)
        return True
    return False


# this helper method handles authorization failure
def handle_auth_failure(response):
    global shutdown
    if response.status_code == 401:
        print("Authentication failed. Please check your USERNAME and PASSWORD.")
        shutdown = True
        return True
    return False


# this helper method compiles possible API responses and handlers
def api_request(session, method, endpoint, params=None):
    while True:
        url = f"{API_ENDPOINT}/{endpoint}"
        if method == 'GET':
            resp = session.get(url, params=params)
        elif method == 'POST':
            resp = session.post(url, params=params)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        if handle_auth_failure(resp):
            return None
        if handle_rate_limit(resp):
            continue
        if resp.ok:
            return resp.json()
        raise ApiException(f"API request failed: {resp.text}")


# Tickers
TICKERS = ["CNR", "RY", "AC"]

# Per case brief
MAX_ORDER_SIZE = 5000    # shares per order
GROSS_LIMIT    = 25000   # sum of the absolute positions; orders that would breach it are rejected
NET_LIMIT      = 25000   # absolute value of the sum of the positions; same

# Fees and rebates in $/share, per case brief. RY is inverted: it pays a rebate for removing
# liquidity and charges a fee for providing it. Not used in this baseline.
ACTIVE_FEE     = {"CNR": 0.0027, "RY": -0.0014, "AC": 0.0015}   # market orders, marketable part of a limit order
PASSIVE_REBATE = {"CNR": 0.0023, "RY": -0.0020, "AC": 0.0011}   # resting limit orders, when filled

# Strategy settings (adjust as needed)
ORDER_SIZE = 500     # shares in each quote; must not exceed MAX_ORDER_SIZE
QUOTE_WAIT = 0.5     # seconds a pair of quotes rests in the book before it is cancelled.
                     # Raise this if you keep seeing the "Rate limit exceeded" message.

# --------- HELPERS ----------
def get_tick_status(session):
    # Gets the tick and the case status (ACTIVE, PAUSED or STOPPED)
    case = api_request(session, 'GET', 'case')
    if case is None:
        return None, None
    return case["tick"], case["status"]

def best_bid_ask(session, ticker):
    # Returns the best bid and ask prices for a ticker; None for a side with no orders
    book = api_request(session, 'GET', 'securities/book', params={"ticker": ticker})
    if book is None:
        return None, None
    bid = book["bids"][0]["price"] if book["bids"] else None
    ask = book["asks"][0]["price"] if book["asks"] else None
    return bid, ask

def positions_map(session):
    # Returns the current position in each ticker
    data = api_request(session, 'GET', 'securities')
    out = {p["ticker"]: int(p.get("position", 0)) for p in (data or [])}
    for t in TICKERS:
        out.setdefault(t, 0)
    return out

def within_limits(pos, ticker, qty):
    # True if a fill of qty shares (positive to buy, negative to sell) would leave gross and net
    # within the limits, or would at least reduce whichever one is already over. Unlike checking
    # the gross position alone, this lets the algo trade back toward flat when it is at a limit.
    after = dict(pos)
    after[ticker] += qty
    gross_now, gross_after = sum(abs(p) for p in pos.values()), sum(abs(p) for p in after.values())
    net_now, net_after = abs(sum(pos.values())), abs(sum(after.values()))
    return ((gross_after <= GROSS_LIMIT or gross_after < gross_now) and
            (net_after <= NET_LIMIT or net_after < net_now))

def place_limit(session, ticker, action, qty, price):
    # Sends a limit order. A rejected order is reported rather than stopping the algo.
    try:
        return api_request(session, 'POST', 'orders',
                           params={"ticker": ticker, "type": "LIMIT", "quantity": int(qty),
                                   "action": action, "price": price}) is not None
    except ApiException as e:
        print(f"{action} {qty} {ticker} @ {price} rejected: {e}")
        return False

def cancel_orders(session, ticker=None):
    # Cancels your open orders in one ticker, or in every ticker if none is given
    params = {"ticker": ticker} if ticker else {"all": 1}
    api_request(session, 'POST', 'commands/cancel', params=params)

# --------- CORE LOGIC ----------
def quote_once(session, ticker):
    # Positions are re-read for each ticker, since fills arrive while quotes rest in the book
    pos = positions_map(session)
    bid, ask = best_bid_ask(session, ticker)

    # Join the best bid and the best ask
    if bid is not None and within_limits(pos, ticker, ORDER_SIZE):
        place_limit(session, ticker, "BUY", ORDER_SIZE, bid)
    if ask is not None and within_limits(pos, ticker, -ORDER_SIZE):
        place_limit(session, ticker, "SELL", ORDER_SIZE, ask)

    # Give the quotes a chance to fill, then cancel what is left. Neither is optimal: a fixed wait
    # is too long when the market is moving and too short when it is not, and cancelling every
    # pass gives up queue priority.
    sleep(QUOTE_WAIT)
    cancel_orders(session, ticker)

def main():
    with requests.Session() as s:
        s.headers.update(AUTHORIZATION)
        started = False
        last_status = None
        while not shutdown:
            try:
                tick, status = get_tick_status(s)
                if status == "ACTIVE":
                    started = True
                    for ticker in TICKERS:
                        if shutdown:
                            break
                        quote_once(s, ticker)
                    pos = positions_map(s)
                    print(f"tick={tick} " + " ".join(f"{t}={pos[t]}" for t in TICKERS)
                          + f" gross={sum(abs(p) for p in pos.values())} net={sum(pos.values())}")
                elif status == "STOPPED" and started:
                    print("Case stopped.")
                    break
                elif status is not None:
                    # Waits for the case to start (or resume), so you can launch the script early
                    if status != last_status:
                        print(f"Case is {status}. Waiting for it to be ACTIVE...")
                    sleep(1)
                last_status = status
            except ApiException as e:
                print(f"API error: {str(e)}")
                sleep(1)
            except requests.exceptions.RequestException as e:
                if not started:
                    raise    # most likely a wrong API_ENDPOINT
                print(f"Connection error, retrying: {e}")
                sleep(1)

        # If stopped with CTRL+C, leave no orders resting in the book
        if shutdown and started:
            try:
                cancel_orders(s)
            except (ApiException, requests.exceptions.RequestException):
                pass

if __name__ == "__main__":
    # register the custom signal handler for graceful shutdowns
    signal.signal(signal.SIGINT, signal_handler)
    try:
        main()
    except requests.exceptions.RequestException as e:
        print(f"Could not connect to {API_ENDPOINT}. Check the address and port, and your internet connection.\n{e}")
    except KeyboardInterrupt:
        print("Stopped.")
