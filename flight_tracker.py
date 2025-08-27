import json
from datetime import datetime, timedelta
import pytz
import time
import schedule
from amadeus import Client, ResponseError
from tenacity import retry, stop_after_attempt, wait_fixed
import smtplib
from email.mime.text import MIMEText
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Environment variables
API_KEY = os.getenv('AMADEUS_API_KEY')
API_SECRET = os.getenv('AMADEUS_API_SECRET')
RECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
SENDER_PASSWORD = os.getenv('SENDER_PASSWORD')

CLASSES = ['ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS']
PREV_DATA_FILE = 'previous_data.json'

# Initialize Amadeus client for test API
# amadeus = Client(client_id=API_KEY, client_secret=API_SECRET, hostname='test')
# print("Initialized Amadeus client for test API (test.api.amadeus.com)")
# For production API, uncomment:
amadeus = Client(client_id=API_KEY, client_secret=API_SECRET, hostname='production')
print("Initialized Amadeus client for production API (api.amadeus.com)")

@retry(stop=stop_after_attempt(3), wait=wait_fixed(5))
def get_flight_details(origin, dest, dep_date, travel_class, is_round=False, return_date=None):
    print(f"Fetching flights for {origin}->{dest} on {dep_date} ({travel_class})" + (" (round trip)" if is_round else ""))
    try:
        params = {
            'originLocationCode': origin,
            'destinationLocationCode': dest,
            'departureDate': dep_date,
            'adults': 1,
            'travelClass': travel_class,
            'max': 50
        }
        if is_round and return_date:
            params['returnDate'] = return_date
        response = amadeus.shopping.flight_offers_search.get(**params)
        print(f"API call successful for {origin}->{dest} on {dep_date} ({travel_class})")
        if response.data:
            print(f"Found {len(response.data)} flights")
            valid_offers = []
            for offer in sorted(response.data, key=lambda x: float(x['price']['total'])):
                if len(offer['itineraries'][0]['segments']) > 1:
                    print(f"Skipping non-direct outbound: {len(offer['itineraries'][0]['segments'])} segments")
                    continue
                baggage = offer.get('travelerPricings', [{}])[0].get('fareDetailsBySegment', [{}])[0].get('includedCheckedBags', {})
                if is_round:
                    if len(offer['itineraries']) < 2 or len(offer['itineraries'][1]['segments']) > 1:
                        print(f"Skipping non-direct return: {len(offer['itineraries'][1]['segments']) if len(offer['itineraries']) > 1 else 'no return'} segments")
                        continue
                    return_baggage = offer.get('travelerPricings', [{}])[0].get('fareDetailsBySegment', [{}])[1].get('includedCheckedBags', {}) if len(offer.get('travelerPricings', [{}])[0].get('fareDetailsBySegment', [{}])) > 1 else {}
                    outbound_arrival = datetime.fromisoformat(offer['itineraries'][0]['segments'][0]['arrival']['at'])
                    return_departure = datetime.fromisoformat(offer['itineraries'][1]['segments'][0]['departure']['at'])
                    layover = return_departure - outbound_arrival
                    layover_hours = layover.total_seconds() / 3600
                    if layover_hours < 7 or layover_hours >= 48:
                        print(f"Skipping round trip with invalid layover {layover} (must be 7–48h)")
                        continue
                    print(f"Valid round trip with layover {layover}")
                    valid_offers.append({
                        'price': float(offer['price']['total']),
                        'outbound_departure': offer['itineraries'][0]['segments'][0]['departure']['at'],
                        'outbound_arrival': offer['itineraries'][0]['segments'][0]['arrival']['at'],
                        'return_departure': offer['itineraries'][1]['segments'][0]['departure']['at'],
                        'return_arrival': offer['itineraries'][1]['segments'][0]['arrival']['at'],
                        'outbound_airline': offer['itineraries'][0]['segments'][0]['carrierCode'],
                        'return_airline': offer['itineraries'][1]['segments'][0]['carrierCode'],
                        'baggage': baggage,
                        'return_baggage': return_baggage,
                        'layover': str(layover)
                    })
                else:
                    if origin == 'LAX' and dest == 'PPT':
                        if not (baggage.get('quantity', 0) >= 1 or baggage.get('weight', 0) >= 1):
                            print(f"Skipping offer with no checked luggage: {baggage}")
                            continue
                        print(f"Valid offer with baggage: {baggage}")
                    valid_offers.append({
                        'price': float(offer['price']['total']),
                        'departure_time': offer['itineraries'][0]['segments'][0]['departure']['at'],
                        'arrival_time': offer['itineraries'][0]['segments'][0]['arrival']['at'],
                        'airline': offer['itineraries'][0]['segments'][0]['carrierCode'],
                        'baggage': baggage
                    })
            if valid_offers:
                return valid_offers[0]  # Return cheapest valid offer
        print(f"No valid flights found for {origin}->{dest} on {dep_date} ({travel_class})")
        return None
    except ResponseError as error:
        print(f"Error for {origin}->{dest} on {dep_date} ({travel_class}): {error.response.body if error.response else error}")
        return None

def fetch_one_way_prices(days=14):  # Reduced to 14 days
    print("Starting fetch_one_way_prices")
    today = datetime.now(pytz.timezone('America/Los_Angeles')).date()
    start_date = today + timedelta(days=1)
    print(f"Using current date (PST): {today}, querying from {start_date}")
    prices = {'PPT-LAX': {}, 'LAX-PPT': {}, 'LAX-PPT-BAG2': {}}
    for i in range(0, days):
        dep_date = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        for cls in CLASSES:
            key = f"{dep_date}-{cls}"
            # PPT -> LAX
            print(f"Querying PPT->LAX: {key}")
            details = get_flight_details('PPT', 'LAX', dep_date, cls)
            if details:
                prices['PPT-LAX'][key] = details
                print(f"Stored PPT->LAX {key}: price {details['price']}")
            else:
                print(f"No data for PPT->LAX {key}")
            time.sleep(1)
            # LAX -> PPT
            print(f"Querying LAX->PPT: {key}")
            details = get_flight_details('LAX', 'PPT', dep_date, cls)
            if details:
                prices['LAX-PPT'][key] = details
                print(f"Stored LAX->PPT {key}: price {details['price']}, baggage {details['baggage']}")
                if details['baggage'].get('quantity', 0) >= 2:
                    prices['LAX-PPT-BAG2'][key] = details
                    print(f"Stored LAX->PPT with bag=2 {key}: price {details['price']}")
            else:
                print(f"No data for LAX->PPT {key}")
            time.sleep(1)
    print(f"Finished fetch_one_way_prices: {len(prices['PPT-LAX'])} PPT->LAX, {len(prices['LAX-PPT'])} LAX->PPT, {len(prices['LAX-PPT-BAG2'])} LAX->PPT-BAG2 entries")
    return prices

def fetch_round_trips(days=14):  # Reduced to 14 days
    print("Starting fetch_round_trips")
    today = datetime.now(pytz.timezone('America/Los_Angeles')).date()
    start_date = today + timedelta(days=1)
    print(f"Using current date (PST): {today}, querying from {start_date}")
    round_prices = {}
    round_prices_bag2 = {}
    for i in range(0, days - 1):  # 13 days for same/next-day returns
        dep_date = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        for cls in CLASSES:
            for delta in [0, 1]:  # Same or next day
                return_date = (start_date + timedelta(days=i + delta)).strftime('%Y-%m-%d')
                key = f"{dep_date}-{return_date}-{cls}"
                print(f"Querying round trip PPT-LAX-PPT: {key}")
                details = get_flight_details('PPT', 'LAX', dep_date, cls, is_round=True, return_date=return_date)
                if details:
                    round_prices[key] = details
                    print(f"Stored round trip {key}: price {details['price']}, layover {details['layover']}, return baggage {details['return_baggage']}")
                    if details['return_baggage'].get('quantity', 0) >= 2:
                        round_prices_bag2[key] = details
                        print(f"Stored round trip with bag=2 on return {key}: price {details['price']}")
                else:
                    print(f"No data for round trip {key}")
                time.sleep(1)
    print(f"Finished fetch_round_trips: {len(round_prices)} round trips, {len(round_prices_bag2)} round trips with bag=2 on return")
    return round_prices, round_prices_bag2

def compute_cheapest(one_way_prices, round_prices, round_prices_bag2):
    print("Starting compute_cheapest")
    cheapest_out = min(one_way_prices['PPT-LAX'].values(), key=lambda x: x['price']) if one_way_prices['PPT-LAX'] else None
    if cheapest_out:
        print(f"Cheapest PPT->LAX: {cheapest_out['price']} (Date: {list(filter(lambda k: one_way_prices['PPT-LAX'][k] is cheapest_out, one_way_prices['PPT-LAX'].keys()))[0]})")
    else:
        print("No PPT->LAX flights found")
    cheapest_ret = min(one_way_prices['LAX-PPT'].values(), key=lambda x: x['price']) if one_way_prices['LAX-PPT'] else None
    if cheapest_ret:
        print(f"Cheapest LAX->PPT (1+ bags): {cheapest_ret['price']} (Date: {list(filter(lambda k: one_way_prices['LAX-PPT'][k] is cheapest_ret, one_way_prices['LAX-PPT'].keys()))[0]})")
    else:
        print("No LAX->PPT (1+ bags) flights found")
    cheapest_ret_bag2 = min(one_way_prices['LAX-PPT-BAG2'].values(), key=lambda x: x['price']) if one_way_prices['LAX-PPT-BAG2'] else None
    if cheapest_ret_bag2:
        print(f"Cheapest LAX->PPT (2+ bags): {cheapest_ret_bag2['price']} (Date: {list(filter(lambda k: one_way_prices['LAX-PPT-BAG2'][k] is cheapest_ret_bag2, one_way_prices['LAX-PPT-BAG2'].keys()))[0]})")
    else:
        print("No LAX->PPT (2+ bags) found")
    min_round_price = float('inf')
    best_round_combo = None
    for key, details in round_prices.items():
        total_price = details['price']
        if total_price < min_round_price:
            min_round_price = total_price
            best_round_combo = details
    if best_round_combo:
        print(f"Cheapest round-trip (7–48h layover): {min_round_price} (Outbound: {best_round_combo['outbound_departure']}, Return: {best_round_combo['return_departure']}, Layover: {best_round_combo['layover']})")
    else:
        print("No valid round-trip (7–48h layover) combinations found")
    min_round_price_bag2 = float('inf')
    best_round_combo_bag2 = None
    for key, details in round_prices_bag2.items():
        total_price = details['price']
        if total_price < min_round_price_bag2:
            min_round_price_bag2 = total_price
            best_round_combo_bag2 = details
    if best_round_combo_bag2:
        print(f"Cheapest round-trip (2+ bags on return, 7–48h layover): {min_round_price_bag2} (Outbound: {best_round_combo_bag2['outbound_departure']}, Return: {best_round_combo_bag2['return_departure']}, Layover: {best_round_combo_bag2['layover']})")
    else:
        print("No valid round-trip (2+ bags on return, 7–48h layover) found")
    min_round_one_way_price = float('inf')
    best_round_one_way_combo = None
    today = datetime.now(pytz.timezone('America/Los_Angeles')).date()
    start_date = today + timedelta(days=1)
    for i in range(0, days - 1):
        out_date_str = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        for out_cls in CLASSES:
            out_key = f"{out_date_str}-{out_cls}"
            if out_key not in one_way_prices['PPT-LAX']:
                continue
            out_details = one_way_prices['PPT-LAX'][out_key]
            for delta in [0, 1]:
                ret_i = i + delta
                if ret_i >= days:
                    continue
                ret_date_str = (start_date + timedelta(days=ret_i)).strftime('%Y-%m-%d')
                for ret_cls in CLASSES:
                    ret_key = f"{ret_date_str}-{ret_cls}"
                    if ret_key not in one_way_prices['LAX-PPT']:
                        continue
                    ret_details = one_way_prices['LAX-PPT'][ret_key]
                    out_arrive = datetime.fromisoformat(out_details['arrival_time'])
                    ret_depart = datetime.fromisoformat(ret_details['departure_time'])
                    if ret_date_str == out_date_str and ret_depart < out_arrive:
                        continue
                    layover = ret_depart - out_arrive
                    layover_hours = layover.total_seconds() / 3600
                    if layover_hours < 7 or layover_hours >= 48:
                        continue
                    total_price = out_details['price'] + ret_details['price']
                    print(f"Checking round-trip equivalent: {out_key} + {ret_key} = {total_price}, Layover: {layover}")
                    if total_price < min_round_one_way_price:
                        min_round_one_way_price = total_price
                        best_round_one_way_combo = {
                            'out_date': out_date_str,
                            'out_class': out_cls,
                            'out_details': out_details,
                            'ret_date': ret_date_str,
                            'ret_class': ret_cls,
                            'ret_details': ret_details,
                            'total_price': total_price,
                            'layover': str(layover)
                        }
    if best_round_one_way_combo:
        print(f"Cheapest round-trip equivalent (7–48h layover): {min_round_one_way_price} (Outbound: {best_round_one_way_combo['out_date']}, Return: {best_round_one_way_combo['ret_date']}, Layover: {best_round_one_way_combo['layover']})")
    else:
        print("No valid round-trip equivalent (7–48h layover) found")
    return {
        'cheapest_out': cheapest_out['price'] if cheapest_out else None,
        'cheapest_out_details': cheapest_out,
        'cheapest_ret': cheapest_ret['price'] if cheapest_ret else None,
        'cheapest_ret_details': cheapest_ret,
        'cheapest_ret_bag2': cheapest_ret_bag2['price'] if cheapest_ret_bag2 else None,
        'cheapest_ret_bag2_details': cheapest_ret_bag2,
        'cheapest_round': min_round_price if min_round_price != float('inf') else None,
        'cheapest_round_details': best_round_combo,
        'cheapest_round_bag2': min_round_price_bag2 if min_round_price_bag2 != float('inf') else None,
        'cheapest_round_bag2_details': best_round_combo_bag2,
        'cheapest_round_one_way': min_round_one_way_price if min_round_one_way_price != float('inf') else None,
        'cheapest_round_one_way_details': best_round_one_way_combo
    }

def load_previous_data():
    print("Loading previous data from", PREV_DATA_FILE)
    try:
        with open(PREV_DATA_FILE, 'r') as f:
            data = json.load(f)
            print("Previous data loaded successfully")
            return data
    except FileNotFoundError:
        print("No previous data file found")
        return {}

def save_data(data):
    print("Saving data to", PREV_DATA_FILE)
    with open(PREV_DATA_FILE, 'w') as f:
        json.dump(data, f)
    print("Data saved successfully")

def detect_changes(new_data, prev_data):
    print("Detecting price changes")
    changes = []
    if new_data['cheapest_out'] != prev_data.get('cheapest_out'):
        details = new_data['cheapest_out_details']
        date, cls = [k for k in one_way_prices['PPT-LAX'] if one_way_prices['PPT-LAX'][k] is details][0].split('-')[:2]
        change_msg = f"Cheapest PPT->LAX one-way changed to {details['price']} (Date: {date}, Class: {cls}, Airline: {details['airline']}, Depart: {details['departure_time']}, Arrive: {details['arrival_time']}, Baggage: {details['baggage']})"
        print(change_msg)
        changes.append(change_msg)
    if new_data['cheapest_ret'] != prev_data.get('cheapest_ret'):
        details = new_data['cheapest_ret_details']
        date, cls = [k for k in one_way_prices['LAX-PPT'] if one_way_prices['LAX-PPT'][k] is details][0].split('-')[:2]
        change_msg = f"Cheapest LAX->PPT one-way (1+ bags) changed to {details['price']} (Date: {date}, Class: {cls}, Airline: {details['airline']}, Depart: {details['departure_time']}, Arrive: {details['arrival_time']}, Baggage: {details['baggage']})"
        print(change_msg)
        changes.append(change_msg)
    if new_data['cheapest_ret_bag2'] != prev_data.get('cheapest_ret_bag2'):
        details = new_data['cheapest_ret_bag2_details']
        date, cls = [k for k in one_way_prices['LAX-PPT-BAG2'] if one_way_prices['LAX-PPT-BAG2'][k] is details][0].split('-')[:2]
        change_msg = f"Cheapest LAX->PPT one-way (2+ bags) changed to {details['price']} (Date: {date}, Class: {cls}, Airline: {details['airline']}, Depart: {details['departure_time']}, Arrive: {details['arrival_time']}, Baggage: {details['baggage']})"
        print(change_msg)
        changes.append(change_msg)
    if new_data['cheapest_round'] != prev_data.get('cheapest_round'):
        d = new_data['cheapest_round_details']
        change_msg = f"Cheapest round-trip (7–48h layover) changed to {d['price']} (Outbound: {d['outbound_departure']} Arrive {d['outbound_arrival']} Airline {d['outbound_airline']} Baggage {d['baggage']}; Return: {d['return_departure']} Arrive {d['return_arrival']} Airline {d['return_airline']} Baggage {d['return_baggage']}; Layover: {d['layover']})"
        print(change_msg)
        changes.append(change_msg)
    if new_data['cheapest_round_bag2'] != prev_data.get('cheapest_round_bag2'):
        d = new_data['cheapest_round_bag2_details']
        change_msg = f"Cheapest round-trip (2+ bags on return, 7–48h layover) changed to {d['price']} (Outbound: {d['outbound_departure']} Arrive {d['outbound_arrival']} Airline {d['outbound_airline']} Baggage {d['baggage']}; Return: {d['return_departure']} Arrive {d['return_arrival']} Airline {d['return_airline']} Baggage {d['return_baggage']}; Layover: {d['layover']})"
        print(change_msg)
        changes.append(change_msg)
    if new_data['cheapest_round_one_way'] != prev_data.get('cheapest_round_one_way'):
        d = new_data['cheapest_round_one_way_details']
        change_msg = f"Cheapest round-trip equivalent (7–48h layover) changed to {d['total_price']} (Outbound: {d['out_date']} {d['out_class']} {d['out_details']['airline']} Depart {d['out_details']['departure_time']} Arrive {d['out_details']['arrival_time']} Baggage {d['out_details']['baggage']}; Return: {d['ret_date']} {d['ret_class']} {d['ret_details']['airline']} Depart {d['ret_details']['departure_time']} Arrive {d['ret_details']['arrival_time']} Baggage {d['ret_details']['baggage']}; Layover: {d['layover']})"
        print(change_msg)
        changes.append(change_msg)
    print(f"Found {len(changes)} changes")
    return changes

def send_email_notification(changes):
    print("Checking for email notification")
    if not changes:
        print("No changes to notify")
        return
    subject = "Flight Price Change Alert: Cheapest PPT-LAX Options"
    body = "Changes in cheapest options:\n\n" + "\n\n".join(changes)
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECIPIENT_EMAIL
    print(f"Sending email to {RECIPIENT_EMAIL}")
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        print("Email sent successfully")
    except Exception as e:
        print(f"Email sending failed: {e}")

def track_flights():
    print("Starting track_flights")
    global one_way_prices
    one_way_prices = fetch_one_way_prices(days=14)
    round_prices, round_prices_bag2 = fetch_round_trips(days=14)
    new_data = compute_cheapest(one_way_prices, round_prices, round_prices_bag2)
    prev_data = load_previous_data()
    changes = detect_changes(new_data, prev_data)
    if changes:
        send_email_notification(changes)
    save_data(new_data)
    print(f"Finished track_flights at {datetime.now(pytz.timezone('America/Los_Angeles'))}. Changes: {len(changes)}")

def run_scheduler():
    print("Starting scheduler")
    schedule.every(6).hours.do(track_flights)  # Run every 6 hours
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    print("Script started")
    run_scheduler()
