"""
RIT Market Simulator Liability Trading 3 (LT3) Case - Support File
Rotman BMO Finance Research and Trading Lab, University of Toronto (C)
All rights reserved.
"""

import os
import itertools
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
This support file does not trade. It shows the depth of the CRZY and TAME order books - the
cumulative volume and VWAP you would get by trading through each level - together with your
positions against the limits and any tender offer waiting for your decision. Use it to judge
whether the book has enough liquidity to unwind a tender at a profit before you accept it.
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

API_ENDPOINT = "http://flserver.rotman.utoronto.ca:PORT/v1"    # LT3 case: replace PORT with your event's port
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
CRZY = "CRZY"
TAME = "TAME"

# Per case brief
NET_LIMIT   = 100000   # absolute value of the sum of the positions
GROSS_LIMIT = 250000   # sum of the absolute positions

# Display settings
BOOK_LEVELS = 20     # levels of each order book to show
REFRESH     = 0.5    # seconds between screen refreshes. Each refresh costs 5 requests;
                     # raise this if you keep seeing the "Rate limit exceeded" message.

# --------- HELPERS ----------
def get_tick_status(session):
    # Gets the tick and the case status (ACTIVE, PAUSED or STOPPED)
    case = api_request(session, 'GET', 'case')
    if case is None:
        return None, None
    return case["tick"], case["status"]

def positions_map(session):
    # Returns the current position in each ticker
    data = api_request(session, 'GET', 'securities')
    out = {p["ticker"]: int(p.get("position", 0)) for p in (data or [])}
    for t in (CRZY, TAME):
        out.setdefault(t, 0)
    return out

def get_tenders(session):
    # Returns the tender offers currently waiting for your decision
    return api_request(session, 'GET', 'tenders') or []

def calculate_cumulatives(side):
    # Adds the cumulative volume and VWAP at each level of one side of an order book: what
    # trading through the book down to that level would get you
    cumulative_vol, cumulative_value = 0, 0.0
    for level in side:
        remaining = level["quantity"] - level["quantity_filled"]
        cumulative_vol += remaining
        cumulative_value += remaining * level["price"]
        level["cumulative_vol"] = int(cumulative_vol)
        level["cumulative_vwap"] = cumulative_value / cumulative_vol if cumulative_vol else 0.0

def get_book(session, ticker):
    # Returns the order book for a ticker, with cumulative volume and VWAP at each level
    book = api_request(session, 'GET', 'securities/book', params={"ticker": ticker, "limit": BOOK_LEVELS})
    if book is None:
        return {"bids": [], "asks": []}
    calculate_cumulatives(book["bids"])
    calculate_cumulatives(book["asks"])
    return book

def clear_screen():
    # 'cls' clears a Windows terminal, 'clear' a Mac or Linux one
    os.system('cls' if os.name == 'nt' else 'clear')

def print_screen(tick, pos, tenders, crzy_book, tame_book):
    # Prints positions, pending tenders and the two order books side by side
    clear_screen()
    net = pos[CRZY] + pos[TAME]
    gross = abs(pos[CRZY]) + abs(pos[TAME])
    print(f"tick {tick} | CRZY {pos[CRZY]:,}  TAME {pos[TAME]:,} | "
          f"net {net:,} / {NET_LIMIT:,} | gross {gross:,} / {GROSS_LIMIT:,}")
    for t in tenders:
        print(f"TENDER {t['tender_id']}: {t.get('caption')} (price {t.get('price')}, expires at tick {t.get('expires')})")
    print()

    print('CRZY                                                           TAME')
    print('BIDVWAP | CUMUVOL |  BID  |  ASK  | CUMUVOL | ASKVWAP          BIDVWAP | CUMUVOL |  BID  |  ASK  | CUMUVOL | ASKVWAP')
    empty = {"cumulative_vwap": 0, "cumulative_vol": 0, "price": 0}
    levels = itertools.zip_longest(crzy_book["bids"], crzy_book["asks"], tame_book["bids"], tame_book["asks"], fillvalue=empty)
    for crzy_bid, crzy_ask, tame_bid, tame_ask in itertools.islice(levels, BOOK_LEVELS):
        print('{:07.4f} | {:07d} | {:05.2f} | {:05.2f} | {:07d} | {:07.4f}          {:07.4f} | {:07d} | {:05.2f} | {:05.2f} | {:07d} | {:07.4f}'.format(
            crzy_bid["cumulative_vwap"], crzy_bid["cumulative_vol"], crzy_bid["price"],
            crzy_ask["price"], crzy_ask["cumulative_vol"], crzy_ask["cumulative_vwap"],
            tame_bid["cumulative_vwap"], tame_bid["cumulative_vol"], tame_bid["price"],
            tame_ask["price"], tame_ask["cumulative_vol"], tame_ask["cumulative_vwap"]))

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
                    print_screen(tick, positions_map(s), get_tenders(s), get_book(s, CRZY), get_book(s, TAME))
                    sleep(REFRESH)
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

if __name__ == "__main__":
    # register the custom signal handler for graceful shutdowns
    signal.signal(signal.SIGINT, signal_handler)
    try:
        main()
    except requests.exceptions.RequestException as e:
        print(f"Could not connect to {API_ENDPOINT}. Check the address and port, and your internet connection.\n{e}")
    except KeyboardInterrupt:
        print("Stopped.")
