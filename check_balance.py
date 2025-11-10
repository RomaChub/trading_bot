from binance.client import Client
import os
import sys
from dotenv import load_dotenv
import requests
import hmac
import hashlib
import time
import json

# Загружаем переменные окружения
load_dotenv()

# Инициализация клиента с вашими API-ключами
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
use_testnet = os.getenv('BINANCE_FUTURES_TESTNET', 'true').lower() == 'false'

if not api_key or not api_secret:
	print("ERROR: BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env file")
	sys.exit(1)

# Создаем клиент с учетом testnet
if use_testnet:
	print("Using Binance Testnet")
	client = Client(api_key, api_secret, testnet=True)
else:
	print("Using Binance Mainnet")
	client = Client(api_key, api_secret)

def get_spot_balance():
	"""Получение спотового баланса с конвертацией в USDT"""
	print("=== СПОТОВЫЙ СЧЕТ ===")
	try:
		# Получаем информацию о счете
		account_info = client.get_account()
		# Получаем все цены пар
		prices = client.get_all_tickers()
		price_dict = {item['symbol']: float(item['price']) for item in prices}

		total_usdt_value = 0.0
		assets_with_balance = []

		for balance in account_info['balances']:
			free = float(balance['free'])
			locked = float(balance['locked'])
			total = free + locked

			if total > 0:
				asset = balance['asset']
				usdt_value = 0.0

				# Если это USDT, просто используем баланс
				if asset == 'USDT':
					usdt_value = total
				else:
					# Пытаемся найти прямую пару с USDT
					usdt_pair = f"{asset}USDT"
					if usdt_pair in price_dict:
						usdt_value = total * price_dict[usdt_pair]
					else:
						# Ищем пару с BTC, а затем конвертируем в USDT
						btc_pair = f"{asset}BTC"
						if btc_pair in price_dict:
							btc_price = price_dict[btc_pair]
							usdt_value = total * btc_price * price_dict.get('BTCUSDT', 0)

				if usdt_value > 0.1:  # Показываем активы с балансом > $0.1
					assets_with_balance.append({
						'asset': asset,
						'total': total,
						'usdt_value': usdt_value
					})
					total_usdt_value += usdt_value

		# Сортируем по убыванию стоимости
		assets_with_balance.sort(key=lambda x: x['usdt_value'], reverse=True)

		for asset_info in assets_with_balance:
			print(f"{asset_info['asset']}: {asset_info['total']:.8f} (${asset_info['usdt_value']:.2f})")

		print(f"Общая стоимость: ${total_usdt_value:.2f}\n")
		return total_usdt_value

	except Exception as e:
		print(f"Ошибка при получении спотового баланса: {e}\n")
		return 0.0

def get_futures_balance():
	"""Получение фьючерсного баланса"""
	print("=== ФЬЮЧЕРСНЫЙ СЧЕТ (USDT-M) ===")
	try:
		# Используем правильный endpoint для фьючерсов
		futures_balance = client.futures_account_balance()

		total_balance = 0.0
		has_balances = False

		for balance in futures_balance:
			asset = balance['asset']
			wallet_balance = float(balance['balance'])

			if wallet_balance > 0:
				print(f"{asset}: {wallet_balance:.8f}")
				if asset == 'USDT':
					total_balance = wallet_balance
				has_balances = True

		if not has_balances:
			print("Нет баланса на фьючерсном счете")
		else:
			print(f"Общий баланс: {total_balance:.8f} USDT\n")

		return total_balance

	except Exception as e:
		print(f"Ошибка при получении фьючерсного баланса: {e}\n")
		return 0.0

def get_futures_account_info():
	"""Получение детальной информации о фьючерсном счете"""
	print("=== ДЕТАЛИ ФЬЮЧЕРСНОГО СЧЕТА ===")
	try:
		account_info = client.futures_account()

		total_wallet_balance = float(account_info['totalWalletBalance'])
		available_balance = float(account_info['availableBalance'])
		unrealized_pnl = float(account_info['totalUnrealizedProfit'])

		print(f"Общий баланс кошелька: {total_wallet_balance:.8f} USDT")
		print(f"Доступный баланс: {available_balance:.8f} USDT")
		print(f"Нереализованная PnL: {unrealized_pnl:.8f} USDT")
		print(f"Общая маржа: {float(account_info['totalMarginBalance']):.8f} USDT\n")

		# Показываем открытые позиции
		positions = [p for p in account_info['positions'] if float(p['positionAmt']) != 0]
		if positions:
			print("Открытые позиции:")
			for pos in positions:
				symbol = pos['symbol']
				amount = float(pos['positionAmt'])
				entry_price = float(pos['entryPrice'])
				unrealized = float(pos['unrealizedProfit'])
				print(f"  {symbol}: {amount} (Вход: {entry_price}, PnL: {unrealized:.8f})")
		print()

		return total_wallet_balance

	except Exception as e:
		print(f"Ошибка при получении информации о фьючерсном счете: {e}\n")
		return 0.0

def get_savings_balance():
	"""Получение баланса сберегательного счета (Earn)"""
	print("=== СБЕРЕГАТЕЛЬНЫЙ СЧЕТ (Earn) ===")
	try:
		# Для запросов к API сберегательного счета
		timestamp = int(time.time() * 1000)
		query_string = f"timestamp={timestamp}"
		signature = hmac.new(
			api_secret.encode('utf-8'),
			query_string.encode('utf-8'),
			hashlib.sha256
		).hexdigest()

		url = f"https://api.binance.com/sapi/v1/lending/daily/token/position?{query_string}&signature={signature}"
		headers = {"X-MBX-APIKEY": api_key}

		response = requests.get(url, headers=headers)
		savings_data = response.json()

		if isinstance(savings_data, list):
			total_savings_value = 0.0
			has_savings = False

			for item in savings_data:
				asset = item['asset']
				total_amount = float(item['totalAmount'])
				if total_amount > 0:
					usdt_value = total_amount  # Упрощенно, для точности нужно конвертировать
					print(f"{asset}: {total_amount:.8f}")
					total_savings_value += usdt_value
					has_savings = True

			if not has_savings:
				print("Нет активов на сберегательном счете")
			else:
				print(f"Общая стоимость: ${total_savings_value:.2f}\n")

			return total_savings_value
		else:
			print("Не удалось получить данные сберегательного счета\n")
			return 0.0

	except Exception as e:
		print(f"Ошибка при получении сберегательного баланса: {e}\n")
		return 0.0

def main():
	"""Основная функция"""
	print("Получение данных аккаунта Binance...\n")

	try:
		# Получаем балансы со всех счетов
		spot_total = get_spot_balance()
		futures_total = get_futures_balance()
		futures_detailed = get_futures_account_info()
		savings_total = get_savings_balance()

		# Итоговая сумма
		print("=== ИТОГО ПО ВСЕМ СЧЕТАМ ===")
		print(f"Спотовый счет: ${spot_total:.2f}")
		print(f"Фьючерсный счет: ${futures_total:.2f}")
		print(f"Сберегательный счет: ${savings_total:.2f}")
		print("-" * 30)
		grand_total = spot_total + futures_total + savings_total
		print(f"ОБЩИЙ БАЛАНС: ${grand_total:.2f}")

	except Exception as e:
		print(f"Критическая ошибка: {e}")

if __name__ == "__main__":
	main()

