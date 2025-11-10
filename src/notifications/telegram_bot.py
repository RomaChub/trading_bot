"""
Telegram bot for trading notifications and statistics
"""
import os
import threading
import time
from typing import Dict, Optional
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()


class TradeStats:
	"""Statistics tracker for trades"""
	
	def __init__(self):
		self.wins = 0  # Выигрышные сделки
		self.losses = 0  # Проигрышные сделки
		self.trailing_wins = 0  # Выигрышные сделки закрытые по трейлинг стопу
		self.total_trades = 0
		self.lock = threading.Lock()
	
	def add_win(self, by_trailing: bool = False):
		"""Add a winning trade"""
		with self.lock:
			self.wins += 1
			self.total_trades += 1
			if by_trailing:
				self.trailing_wins += 1
	
	def add_loss(self):
		"""Add a losing trade"""
		with self.lock:
			self.losses += 1
			self.total_trades += 1
	
	def get_stats(self) -> Dict:
		"""Get current statistics"""
		with self.lock:
			win_rate = (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0
			return {
				"wins": self.wins,
				"losses": self.losses,
				"trailing_wins": self.trailing_wins,
				"total_trades": self.total_trades,
				"win_rate": win_rate
			}
	
	def reset(self):
		"""Reset statistics"""
		with self.lock:
			self.wins = 0
			self.losses = 0
			self.trailing_wins = 0
			self.total_trades = 0


class TelegramNotifier:
	"""Telegram bot for sending notifications"""
	
	def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
		self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
		self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
		self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
		self.stats = TradeStats()
		# Bot is enabled if we have bot_token (for commands)
		self.enabled = bool(self.bot_token)
		# Notifications are enabled only if we have both bot_token and chat_id
		self.notifications_enabled = bool(self.bot_token and self.chat_id)
		
		if not self.enabled:
			print("⚠️ Telegram bot disabled: BOT_TOKEN not set")
		elif not self.notifications_enabled:
			print("✅ Telegram bot enabled (commands only)")
			print("⚠️ Telegram notifications disabled: CHAT_ID not set")
			# Start bot polling in background thread for commands
			self.polling_thread = None
			self.running = False
			self.start_polling()
		else:
			print("✅ Telegram bot enabled (commands + notifications)")
			# Start bot polling in background thread
			self.polling_thread = None
			self.running = False
			self.start_polling()
	
	def start_polling(self):
		"""Start polling for bot commands"""
		if not self.enabled:
			return
		self.running = True
		self.polling_thread = threading.Thread(target=self._poll_commands, daemon=True)
		self.polling_thread.start()
		print("✅ Telegram bot polling started")
	
	def _poll_commands(self):
		"""Poll for bot commands"""
		last_update_id = 0
		error_count = 0
		print(f"[Telegram] Starting polling loop...")
		
		# First, delete webhook if exists (to avoid 409 conflict)
		try:
			delete_response = requests.get(f"{self.base_url}/deleteWebhook", timeout=5)
			if delete_response.status_code == 200:
				print("[Telegram] Webhook deleted (if existed)")
		except:
			pass
		
		while self.running:
			try:
				response = requests.get(
					f"{self.base_url}/getUpdates",
					params={"offset": last_update_id + 1, "timeout": 10, "allowed_updates": ["message"]},
					timeout=15
				)
				if response.status_code == 200:
					data = response.json()
					if data.get("ok") and data.get("result"):
						for update in data["result"]:
							last_update_id = update["update_id"]
							if "message" in update:
								message = update["message"]
								text = message.get("text", "").strip()
								chat_id = str(message["chat"]["id"])
								
								print(f"[Telegram] Received message: {text} from chat_id: {chat_id}")
								
								# Handle /start command
								if text == "/start" or text.startswith("/start"):
									welcome_msg = """🤖 Trading Bot

Доступные команды:
/stats - Показать статистику торговли
/reset_stats - Сбросить статистику

Бот работает в режиме polling и готов к работе!"""
									self.send_message(chat_id, welcome_msg, parse_mode=None)
									# If chat_id was not set, save it from first message
									if not self.chat_id:
										self.chat_id = chat_id
										self.notifications_enabled = bool(self.bot_token and self.chat_id)
										if self.notifications_enabled:
											print(f"✅ Chat ID saved from /start command: {chat_id}")
											self.send_message(chat_id, "✅ Уведомления активированы!", parse_mode=None)
								elif text == "/stats":
									self._send_stats(chat_id)
								elif text == "/reset_stats":
									self.stats.reset()
									self.send_message(chat_id, "📊 Статистика сброшена", parse_mode=None)
					else:
						# Check for errors in response
						if not data.get("ok"):
							error_desc = data.get("description", "Unknown error")
							print(f"[Telegram] ⚠️ API error: {error_desc}")
				elif response.status_code == 409:
					# Conflict - webhook exists or another process is polling
					print(f"[Telegram] ⚠️ HTTP 409: Conflict detected. Trying to delete webhook...")
					try:
						delete_response = requests.get(f"{self.base_url}/deleteWebhook", timeout=5)
						if delete_response.status_code == 200:
							print("[Telegram] ✅ Webhook deleted, retrying...")
							time.sleep(2)
							continue
					except Exception as e:
						print(f"[Telegram] ⚠️ Failed to delete webhook: {e}")
					error_count += 1
					if error_count > 5:
						print(f"[Telegram] ❌ Too many 409 errors, stopping polling")
						break
				else:
					print(f"[Telegram] ⚠️ HTTP error: {response.status_code}")
					if response.status_code == 200:
						try:
							error_data = response.json()
							if not error_data.get("ok"):
								print(f"[Telegram] ⚠️ API error: {error_data.get('description', 'Unknown')}")
						except:
							pass
					error_count += 1
					if error_count > 10:
						print(f"[Telegram] ❌ Too many errors, stopping polling")
						break
			except requests.exceptions.RequestException as e:
				error_count += 1
				if error_count % 10 == 0:  # Log every 10th error
					print(f"[Telegram] ⚠️ Connection error (count: {error_count}): {e}")
				if error_count > 50:
					print(f"[Telegram] ❌ Too many connection errors, stopping polling")
					break
			except Exception as e:
				error_count += 1
				print(f"[Telegram] ⚠️ Unexpected error: {e}")
				if error_count > 20:
					print(f"[Telegram] ❌ Too many errors, stopping polling")
					break
			time.sleep(1)
	
	def _send_stats(self, chat_id: str):
		"""Send statistics to chat"""
		stats = self.stats.get_stats()
		message = f"""📊 Статистика торговли

✅ Вин: {stats['wins']}
❌ Лосов: {stats['losses']}
🎯 По трейлингу: {stats['trailing_wins']}
📈 Всего сделок: {stats['total_trades']}
📊 Винрейт: {stats['win_rate']:.2f}%
"""
		self.send_message(chat_id, message, parse_mode=None)
	
	def send_message(self, chat_id: str, message: str, parse_mode: Optional[str] = None):
		"""Send message to Telegram chat (non-blocking)"""
		if not self.enabled or not self.base_url:
			return
		
		if not chat_id:
			return
		
		# Отправляем в отдельном потоке, чтобы не блокировать основной код
		def _send():
			try:
				payload = {
					"chat_id": chat_id,
					"text": message
				}
				# Only add parse_mode if specified (to avoid Markdown parsing errors)
				if parse_mode:
					payload["parse_mode"] = parse_mode
				
				response = requests.post(
					f"{self.base_url}/sendMessage",
					json=payload,
					timeout=5
				)
				if response.status_code != 200:
					try:
						error_data = response.json()
						error_desc = error_data.get("description", response.text)
						print(f"⚠️ Failed to send Telegram message: {error_desc}")
					except:
						print(f"⚠️ Failed to send Telegram message: {response.status_code} - {response.text}")
			except Exception as e:
				print(f"⚠️ Error sending Telegram message: {e}")
		
		# Запускаем в отдельном потоке (daemon=True чтобы не блокировать завершение программы)
		thread = threading.Thread(target=_send, daemon=True)
		thread.start()
	
	def notify_position_opened(self, symbol: str, direction: str, entry_price: float, 
	                          quantity: float, stop_loss: float, take_profit: float, zone_id: int):
		"""Notify about opened position"""
		if not self.notifications_enabled:
			return
		message = f"""🚀 Позиция открыта

📊 Пара: {symbol}
📈 Направление: {direction}
💰 Вход: ${entry_price:.2f}
📦 Количество: {quantity:.6f}
🛑 Стоп: ${stop_loss:.2f}
🎯 Тейк: ${take_profit:.2f}
🏷️ Зона: {zone_id}
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
		self.send_message(self.chat_id, message, parse_mode=None)
	
	def notify_position_closed(self, symbol: str, direction: str, entry_price: float,
	                          exit_price: float, quantity: float, pnl: float, 
	                          by_trailing: bool = False, reason: str = ""):
		"""Notify about closed position"""
		# Always update stats (even if notifications are disabled)
		is_win = pnl > 0
		if is_win:
			self.stats.add_win(by_trailing=by_trailing)
		else:
			self.stats.add_loss()
		
		# Send notification only if enabled
		if not self.notifications_enabled:
			return
		
		emoji = "✅" if is_win else "❌"
		trailing_emoji = "🎯" if by_trailing else ""
		
		message = f"""{emoji} Позиция закрыта {trailing_emoji}

📊 Пара: {symbol}
📈 Направление: {direction}
💰 Вход: ${entry_price:.2f}
💰 Выход: ${exit_price:.2f}
📦 Количество: {quantity:.6f}
💵 P&L: ${pnl:.2f} ({'+' if pnl > 0 else ''}{pnl/abs(entry_price * quantity) * 100:.2f}%)
"""
		if by_trailing:
			message += f"🎯 Закрыто по трейлинг стопу\n"
		if reason:
			message += f"📝 Причина: {reason}\n"
		message += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
		
		self.send_message(self.chat_id, message, parse_mode=None)
	
	def notify_trailing_activated(self, symbol: str, direction: str, entry_price: float,
	                              current_price: float, stop_price: float, rr_ratio: float):
		"""Notify about trailing stop activation"""
		if not self.notifications_enabled:
			return
		message = f"""🎯 Трейлинг стоп активирован

📊 Пара: {symbol}
📈 Направление: {direction}
💰 Вход: ${entry_price:.2f}
📊 Текущая цена: ${current_price:.2f}
🛑 Стоп: ${stop_price:.2f}
📈 RR: {rr_ratio:.2f}
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
		self.send_message(self.chat_id, message, parse_mode=None)
	
	def stop(self):
		"""Stop the bot"""
		self.running = False
		if self.polling_thread:
			self.polling_thread.join(timeout=2)

