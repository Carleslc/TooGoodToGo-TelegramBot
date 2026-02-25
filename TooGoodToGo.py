import json
import time
from configparser import SectionProxy

from pathlib import Path
from _thread import start_new_thread
from datetime import datetime, timedelta
from pytz import timezone, utc
from babel.numbers import format_currency

import tgtg

from tgtg import TgtgClient
from telebot import TeleBot, types

class TooGoodToGo:

    ITEM_STATUS = {
        'sold_out': 'Sold out',
        'new_stock': 'New stock',
        'stock_reduced': 'Stock reduced',
        'stock_increased': 'Stock increased',
    }

    users_login_data = {}
    users_settings_data = {}
    available_items_favorites = {}
    connected_clients = {}

    def __init__(self, bot_token: str, config: SectionProxy = {}):
        self.bot = TeleBot(bot_token)

        self.__set_config(config)

        self.read_users_login_data_from_txt()
        self.read_users_settings_data_from_txt()
        self.read_available_items_favorites_from_txt()

        start_new_thread(self.get_available_items_per_user, ())

        self.bot.set_my_commands([
            types.BotCommand("/info", "favorite bags currently available"),
            types.BotCommand("/login", "log in with your email"),
            types.BotCommand("/settings", "set when you want to be notified"),
            types.BotCommand("/help", "Help dialog"),
            types.BotCommand("/sleep", "Silence the bot for a while"),
        ])
    
    def __set_config(self, config: SectionProxy):
        self.timezone = timezone(config.get('timezone', 'UTC'))
        print('timezone', self.timezone)

        self.language = config.get('language', 'en-GB')
        print('language', self.language)
        
        self.date_format = config.get('date_format', '%a %d.%m at %H:%M')
        print('date_format', self.date_format)

        # min 2 default 5
        self.login_timeout_minutes = max(2, int(config.get('login_timeout_minutes', 5)))
        print('login_timeout_minutes', self.login_timeout_minutes)

        tgtg.MAX_POLLING_TRIES = (self.login_timeout_minutes * 60) // tgtg.POLLING_WAIT_TIME

        # min 5 default 60
        self.interval_seconds = max(5, int(config.get('interval_seconds', 60)))
        print('interval_seconds', self.interval_seconds)

        # min self.interval_seconds default 1800
        self.low_hours_interval_seconds = max(self.interval_seconds, int(config.get('low_hours_interval_seconds', 1800)))
        print('low_hours_interval_seconds', self.low_hours_interval_seconds)

        # min 0 max 23 default 23
        self.low_hours_start = max(0, min(23, int(config.get('low_hours_start', 23))))
        
        # min 0 max 23 default 6
        self.low_hours_end = max(0, min(23, int(config.get('low_hours_end', 6))))
        print('Low hours', self.low_hours_start, '-', self.low_hours_end)

    def send_message(self, telegram_user_id, message, parse_mode=None):
        self.bot.send_message(telegram_user_id, text=message, parse_mode=parse_mode)

    def send_message_with_link(self, telegram_user_id, message, item_id):
        self.bot.send_message(telegram_user_id, text=message, reply_markup=types.InlineKeyboardMarkup(
            keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="Open in app 📱",
                        callback_data="open_app",
                        url="https://share.toogoodtogo.com/item/" + item_id
                    )
                ],
            ])
        )

    def read_users_login_data_from_txt(self):
        with open(data_file('users_login_data'), 'r') as file:
            self.users_login_data = json.load(file, cls=DateTimeDecoder)

    def save_users_login_data_to_txt(self):
        with open(data_file('users_login_data'), 'w') as file:
            json.dump(self.users_login_data, file, cls=DateTimeEncoder)

    def read_users_settings_data_from_txt(self):
        with open(data_file('users_settings_data'), 'r') as file:
            self.users_settings_data = json.load(file)

    def save_users_settings_data_to_txt(self):
        with open(data_file('users_settings_data'), 'w') as file:
            json.dump(self.users_settings_data, file)

    def read_available_items_favorites_from_txt(self):
        with open(data_file('available_items_favorites'), 'r') as file:
            self.available_items_favorites = json.load(file)

    def save_available_items_favorites_to_txt(self):
        with open(data_file('available_items_favorites'), 'w') as file:
            json.dump(self.available_items_favorites, file)

    def add_user(self, login_client, telegram_user_id, telegram_username, credentials):
        credentials['email'] = login_client.email
        credentials['telegram_username'] = telegram_username
        credentials['last_time_token_refreshed'] = login_client.last_time_token_refreshed

        self.users_login_data[telegram_user_id] = credentials
        self.save_users_login_data_to_txt()

        if telegram_user_id not in self.users_settings_data:
            self.users_settings_data[telegram_user_id] = {
                'sold_out': 0,
                'new_stock': 1,
                'stock_reduced': 0,
                'stock_increased': 0
            }
            self.save_users_settings_data_to_txt()

    # Get the credentials
    def new_user(self, telegram_user_id, telegram_username, email):
        client = TgtgClient(email=email, language=self.language)

        self.send_message(telegram_user_id, "📩 Please open your mail account."
                                    "\nYou will receive an email with a confirmation link."
                                    "\n_Opening email on mobile won't work if you have installed TooGoodToGo app._\n"
                                    "\n*You must open the link in your PC browser.*"
                                    "\n_You do not need to enter a password._", parse_mode="markdown")

        try:
            credentials = client.get_credentials() # login
            self.add_user(client, telegram_user_id, telegram_username, credentials)
            self.connect(telegram_user_id)
            self.send_message(telegram_user_id, "✅ You are now logged in!")
        except tgtg.exceptions.TgtgPollingError as err:
            if 'Max retries' in str(err):
                self.send_message(telegram_user_id, "⏱ *Time expired. Please log in again.*", parse_mode="markdown")
            else:
                raise err
        except tgtg.exceptions.TgtgAPIError as err:
            self.handle_api_error(err, telegram_user_id, client)
            self.send_message(telegram_user_id, "❌ Cannot log in. Please try again later.")
        except Exception as err:
            print(f"Unexpected {err=}, {type(err)=}")
            self.send_message(telegram_user_id, "❌ An error happened while logging in. Please try again.")
    
    # Save refreshed tokens and cookies
    def update_credentials(self, telegram_user_id, refresh=False, client=None):
        if not client:
            client = self.get_client(telegram_user_id)

            if not client:
                return False

        if refresh:
            client.login() # tokens may need to be refreshed
        
        user_credentials = self.find_credentials_by_telegramUserID(telegram_user_id)

        if user_credentials and user_credentials['last_time_token_refreshed'] < client.last_time_token_refreshed:
            user_credentials['last_time_token_refreshed'] = client.last_time_token_refreshed
            user_credentials['access_token'] = client.access_token
            user_credentials['refresh_token'] = client.refresh_token
            user_credentials['cookie'] = client.cookie

            self.save_users_login_data_to_txt()

            print(f"{telegram_user_id} token refreshed")
        
        return True

    # Look if the user is already logged in
    def find_credentials_by_telegramUserID(self, user_id):
        return self.users_login_data.get(user_id)

    # Checks if a connection already exists, or if it has to be created initially.
    def connect(self, user_id):
        client = self.get_client(user_id)

        if not client:
            user_credentials = self.find_credentials_by_telegramUserID(user_id)

            if not user_credentials:
                return None
            
            print(f"Connect {user_id}")
            client = TgtgClient(user_id=user_credentials["user_id"],
                                access_token=user_credentials["access_token"],
                                refresh_token=user_credentials["refresh_token"],
                                last_time_token_refreshed=user_credentials["last_time_token_refreshed"],
                                cookie=user_credentials["cookie"],
                                language=self.language)
            self.connected_clients[user_id] = client
            time.sleep(2)

        return client
    
    def get_client(self, user_id):
        return self.connected_clients.get(user_id)

    def get_favourite_items(self, telegram_user_id):
        client = self.connect(telegram_user_id)

        if not client:
            return None

        favourite_items = client.get_items(favorites_only=True)

        self.update_credentials(telegram_user_id, client=client)

        return favourite_items

    # /info command
    def send_available_favourite_items_for_one_user(self, user_id):
        try:
            available_items = []
            favourite_items = self.get_favourite_items(user_id)
            for item in favourite_items:
                if item['items_available'] > 0:
                    item_id = item['item']['item_id']
                    item_text = self.format_item(item)
                    self.send_message_with_link(user_id, item_text, item_id)
                    available_items.append(item_id)
            if not favourite_items:
                self.send_message(user_id, "You do not have any favorites to track yet")
            elif not available_items:
                self.send_message(user_id, "Currently all your favorites are sold out 😕")
        except tgtg.exceptions.TgtgAPIError as err:
            self.send_message(user_id, "❌ Cannot retrieve your favourites. Please try again later.")
            self.handle_api_error(err, user_id)
        except Exception as err:
            print(f"Unexpected {err=}, {type(err)=}")
            self.send_message(user_id, "❌ An error happened trying to retrieve your favourites. Please try again.")
    
    def handle_api_error(self, err, user_id, client=None):
        if len(err.args) == 2:
            status, message = err.args

            if status in [401, 403]:
                print(f"API Unauthorized [{status}]: {message}")
                
                if not client:
                    client = self.get_client(user_id)
                
                print('Headers', client._headers if client else None)

                if status == 401:
                    user_credentials = self.find_credentials_by_telegramUserID(user_id)

                    if user_credentials:
                        del self.connected_clients[user_id]
                        del self.users_login_data[user_id]
                        self.save_users_login_data_to_txt()
                        print("Expired user login data:", user_id)
                        
                        self.send_message(user_id, f"Hello, {user_credentials['telegram_username']}, your session expired, please /login again to continue receiving notifications.")
            else:
                print(f"API Error [{status}]: {message}")
        else:
            print(f"Unexpected API Error: {err=}")

    def __get_tax_percentage(self, item):
        tax_map_list = item['item']['sales_taxes']
        tax_percentage = 0
        for tax_map in tax_map_list:
            tax_percentage += tax_map['tax_percentage']

        return tax_percentage / 100

    def __get_price(self, item):
        item_price_code = item['item']['item_price']['code']

        if item['item']['taxation_policy'] == 'PRICE_DOES_NOT_INCLUDE_TAXES':
            tax_percentage = self.__get_tax_percentage(item)
            item_price = self.__get_currency(item['item']['price_excluding_taxes']) * (1 + tax_percentage)
        else:
            item_price = self.__get_currency(item['item']['price_including_taxes'])

        item_price_string = format_currency(item_price, item_price_code)
        return item_price_string

    def __get_value(self, item):
        item_price_code = item['item']['item_price']['code']

        if item['item']['taxation_policy'] == 'PRICE_DOES_NOT_INCLUDE_TAXES':
            tax_percentage = self.__get_tax_percentage(item)
            item_price = self.__get_currency(item['item']['value_excluding_taxes']) * (1 + tax_percentage)
        else:
            item_price = self.__get_currency(item['item']['value_including_taxes'])

        item_price_string = format_currency(item_price, item_price_code)
        return item_price_string

    def __get_currency(self, price_map):
        currency_decimals = price_map['decimals']
        currency_minor_units = int(price_map['minor_units'])
        return currency_minor_units / (10 ** currency_decimals)
    
    def format_item(self, item, status = None, user_id = None) -> str:
        store_name = item['store']['store_name'].strip()
        store_name_text = f"🍽 {store_name}"
        store_address_line = f"🧭 {item['store']['store_location']['address']['address_line']}"
        store_items_available = item['items_available']
        store_items_available_text = f"🥡 {item['items_available']}"
        item_price_string = f"💰 {self.__get_price(item)} -- ({self.__get_value(item)} value)"

        item_text = f"{store_name_text}\n{store_address_line}\n{item_price_string}\n{store_items_available_text}"

        if store_items_available > 0:
            store_pickup_start = self.__format_datetime(item['pickup_interval']['start'])
            store_pickup_end = self.__format_datetime(item['pickup_interval']['end'])
            store_pickup_text = f"⏰ {store_pickup_start} - {store_pickup_end}"
            item_text += '\n' + store_pickup_text
        
        if status:
            status = self.format_status(status)
            item_text += '\n' + status
            if user_id:
                item_id = item['item']['item_id']
                print(f"[{user_id}] {status} {store_items_available_text} 🍽  {store_name} ({item_id})")
        
        return item_text

    def format_status(self, status: str) -> str:
        return TooGoodToGo.ITEM_STATUS[status]

    def get_available_items_per_user(self):
        """Loop through all users and see if the number of their favorite bags has changed"""
        while True:
            changed_items_status = {}
            available_items_before = len(self.available_items_favorites)
            
            for user_id in self.users_login_data:
                try:
                    user_settings = self.users_settings_data[user_id]

                    # if any alert is enabled for this user
                    if not self.is_silenced(user_id) and any(setting == 1 for setting in user_settings.values()):
                        favourite_items = self.get_favourite_items(user_id)

                        for item in favourite_items:
                            item_id = item['item']['item_id']
                            status = changed_items_status.get(item_id)

                            if status is None and item_id in self.available_items_favorites:
                                old_items_available = int(self.available_items_favorites[item_id]['items_available'])
                                new_items_available = int(item['items_available'])
                                if new_items_available == 0 and old_items_available > 0:  # Sold out (x -> 0)
                                    status = 'sold_out'
                                elif old_items_available == 0 and new_items_available > 0:  # New Bag available (0 -> x)
                                    status = 'new_stock'
                                elif new_items_available < old_items_available:  # Reduced stock available (x -> x-1)
                                    status = 'stock_reduced'
                                elif new_items_available > old_items_available:  # Increased stock available (x -> x+1)
                                    status = 'stock_increased'

                            if status:
                                changed_items_status[item_id] = status

                                if user_settings[status]:
                                    item_text = self.format_item(item, status, user_id)
                                    self.send_message_with_link(user_id, item_text, item_id)
                            
                            self.available_items_favorites[item_id] = item
                except tgtg.exceptions.TgtgAPIError as err:
                    self.handle_api_error(err, user_id)
            
            try:
                if changed_items_status or len(self.available_items_favorites) != available_items_before:
                    self.save_available_items_favorites_to_txt()
            except Exception as err:
                print(f"Unexpected {err=}, {type(err)=}")
            
            # wait until next check
            time.sleep(self.get_interval_seconds())
    
    def get_interval_seconds(self):
        low_hours = False
        now = datetime.now(self.timezone)
        current_hour = now.hour
        # e.g. 0 - 6
        if self.low_hours_start <= self.low_hours_end:
            low_hours = self.low_hours_start <= current_hour < self.low_hours_end
        # e.g. 23 - 6
        elif current_hour >= self.low_hours_start or current_hour < self.low_hours_end:
            low_hours = True
        if low_hours:
            next_hour = (current_hour + 1) % 24
            if next_hour >= self.low_hours_end:
                # adjust interval to the minimum required when low hours are ending
                remaining_minutes = 59 - now.minute
                remaining_seconds = 60 - now.second
                time_to_end_low_hours = remaining_minutes * 60 + remaining_seconds
                if time_to_end_low_hours < self.low_hours_interval_seconds:
                    return max(time_to_end_low_hours, self.interval_seconds)
            return self.low_hours_interval_seconds
        return self.interval_seconds
    
    def __format_datetime(self, datetime_str: str) -> str:
        return (datetime.strptime(datetime_str, '%Y-%m-%dT%H:%M:%SZ')
                .replace(tzinfo=utc)
                .astimezone(self.timezone)
                .strftime(self.date_format))

    def silence_for_user(self, chat_id, secs=0, minutes=0, hours=0, days=0):
        now = datetime.now()
        exp = now + timedelta(seconds=secs, minutes=minutes, hours=hours, days=days)

        self.users_settings_data[chat_id]['silence_exp'] = exp.isoformat()
        self.save_users_settings_data_to_txt()


    def is_silenced(self, chat_id):
        silence_exp_string = self.users_settings_data[chat_id].get('silence_exp')
        if silence_exp_string is None:
            return False
        silence_exp = datetime.fromisoformat(silence_exp_string)
        if silence_exp < datetime.now():
            print(f"{chat_id} Silence has expired. Exp: {silence_exp_string}")
            del self.users_settings_data[chat_id]['silence_exp']
            self.save_users_settings_data_to_txt()
            return False
        return True


def data_file(data_file_name: str, data_folder='data', extension='json') -> Path:
    data_path = Path(f'{data_folder}/{data_file_name}.{extension}')
    if not data_path.exists():
        data_path.parent.mkdir(exist_ok=True, parents=True)
        data_path.write_text('{}')
        print(f'Created {data_path}')
    return data_path

class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.astimezone(utc).isoformat()
        return super().default(obj)

class DateTimeDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, object_hook=self.object_hook, **kwargs)

    def object_hook(self, obj):
        for key, value in obj.items():
            if key == 'last_time_token_refreshed':
                obj[key] = datetime.fromisoformat(value) \
                    .astimezone().replace(tzinfo=None)
                    # tgtg model login uses local timezone naive datetime.now() to compare refresh token time
                    # tgtg/__init__.py", line 121, in _refresh_token
        return obj
