"""
Live chart module for real-time trading visualization
Shows accumulation zones, entry points, and current price
"""
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Dash, dcc, html
from dash.dependencies import Input, Output, State


class LiveChart:
	"""Live interactive chart for trading strategy visualization"""
	
	def __init__(self, symbol: str, update_interval: int = 5, port: int = 8050):
		self.symbol = symbol
		self.update_interval = update_interval
		self.port = port
		self.df: Optional[pd.DataFrame] = None
		self.zones: List[Dict] = []
		self.entry_points: List[Dict] = []
		self.current_price: float = 0.0
		self.is_running = False
		self.thread: Optional[threading.Thread] = None
		self.fig: Optional[go.Figure] = None
		self.app: Optional[Dash] = None
		self.saved_layout: Optional[Dict] = None  # Сохраняем состояние зума и выделений
		
	def update_data(self, df: pd.DataFrame = None, zones: List[Dict] = None, 
	                entry_point: Dict = None, current_price: float = None):
		"""Update chart data"""
		if df is not None:
			self.df = df.copy()
		if zones is not None:
			self.zones = zones
		if entry_point is not None:
			self.entry_points.append(entry_point)
		if current_price is not None:
			self.current_price = current_price
		
	def _create_chart(self) -> go.Figure:
		"""Create or update the chart figure"""
		if self.df is None or self.df.empty:
			# Create empty chart
			fig = go.Figure()
			fig.update_layout(
				title=f"{self.symbol} - Live Chart (No data yet)",
				xaxis_title="Time",
				yaxis_title="Price (USDT)",
				height=800
			)
			return fig
		
		# Create subplots: price chart and volume
		fig = make_subplots(
			rows=2, cols=1,
			shared_xaxes=True,
			vertical_spacing=0.03,
			row_heights=[0.7, 0.3],
			subplot_titles=(f"{self.symbol} - Live Chart", "Volume")
		)
		
		# Get last 200 candles for better performance
		# Always show the most recent data
		if len(self.df) > 200:
			display_df = self.df.tail(200).copy()
		else:
			display_df = self.df.copy()
		
		# Plot candlesticks
		fig.add_trace(
			go.Candlestick(
				x=display_df.index,
				open=display_df['open'],
				high=display_df['high'],
				low=display_df['low'],
				close=display_df['close'],
				name='Price',
				increasing_line_color='#26a69a',
				decreasing_line_color='#ef5350'
			),
			row=1, col=1
		)
		
		# Filter zones: show only newest zone or zones with active trades
		active_zone_ids = {entry.get('zone_id', -1) for entry in self.entry_points}
		
		# Find newest zone (by end time)
		newest_zone = None
		newest_zone_end = None
		for zone in self.zones:
			try:
				zone_end = pd.Timestamp(zone.get('end'))
				if newest_zone_end is None or zone_end > newest_zone_end:
					newest_zone_end = zone_end
					newest_zone = zone
			except:
				continue
		
		# Plot accumulation zones - only newest or zones with active trades
		for zone in self.zones:
			try:
				zone_start = pd.Timestamp(zone.get('start'))
				zone_end = pd.Timestamp(zone.get('end'))
				zone_high = float(zone.get('high', 0))
				zone_low = float(zone.get('low', 0))
				zone_id = zone.get('zone_id', 0)
				
				# Filter: show only newest zone or zones with active trades
				is_newest = (newest_zone is not None and zone_id == newest_zone.get('zone_id', -1))
				has_active_trade = zone_id in active_zone_ids
				
				if not (is_newest or has_active_trade):
					continue
				
				# Check if zone overlaps with display data
				if zone_end < display_df.index[0] or zone_start > display_df.index[-1]:
					continue
				
				# Create zone rectangle
				zone_start_display = max(zone_start, display_df.index[0])
				zone_end_display = min(zone_end, display_df.index[-1])
				
				# Add zone rectangle using scatter
				fig.add_trace(
					go.Scatter(
						x=[zone_start_display, zone_end_display, zone_end_display, zone_start_display, zone_start_display],
						y=[zone_low, zone_low, zone_high, zone_high, zone_low],
						fill='toself',
						fillcolor='rgba(255, 255, 0, 0.3)',
						line=dict(color='orange', width=2, dash='dash'),
						mode='lines',
						name=f'Zone {zone_id}',
						showlegend=True,
						hovertext=f'Zone {zone_id}: ${zone_low:.2f} - ${zone_high:.2f}',
						hoverinfo='text',
						legendgroup=f'zone_{zone_id}'
					),
					row=1, col=1
				)
				
				# Add zone boundaries (only show if zone is visible)
				if zone_start_display <= display_df.index[-1] and zone_end_display >= display_df.index[0]:
					fig.add_hline(
						y=zone_high,
						line_dash="dash",
						line_color="orange",
						opacity=0.7,
						annotation_text=f"Zone {zone_id} High: ${zone_high:.2f}",
						annotation_position="left",
						row=1, col=1
					)
					fig.add_hline(
						y=zone_low,
						line_dash="dash",
						line_color="orange",
						opacity=0.7,
						annotation_text=f"Zone {zone_id} Low: ${zone_low:.2f}",
						annotation_position="left",
						row=1, col=1
					)
			except Exception as e:
				# Skip zone if there's an error
				continue
		
		# Plot entry points
		for entry in self.entry_points:
			entry_time = pd.Timestamp(entry.get('time', datetime.now()))
			entry_price = float(entry.get('price', 0))
			direction = entry.get('direction', 'LONG')
			zone_id = entry.get('zone_id', 0)
			stop_loss = entry.get('stop_loss', 0)
			take_profit = entry.get('take_profit', 0)
			
			# Check if entry is in display range
			if entry_time < display_df.index[0] or entry_time > display_df.index[-1]:
				continue
			
			# Entry point marker
			color = '#00ff00' if direction == 'LONG' else '#ff0000'
			marker_symbol = 'triangle-up' if direction == 'LONG' else 'triangle-down'
			
			fig.add_trace(
				go.Scatter(
					x=[entry_time],
					y=[entry_price],
					mode='markers+text',
					marker=dict(
						size=20,
						color=color,
						symbol=marker_symbol,
						line=dict(width=2, color='black')
					),
					text=[f"{direction}<br>Entry: ${entry_price:.2f}<br>Zone: {zone_id}"],
					textposition="top center",
					name=f'Entry {direction}',
					showlegend=True,
					hovertext=f'{direction} Entry @ ${entry_price:.2f} (Zone {zone_id})',
					hoverinfo='text'
				),
				row=1, col=1
			)
			
			# Stop loss line
			if stop_loss > 0:
				fig.add_hline(
					y=stop_loss,
					line_dash="dot",
					line_color="red",
					opacity=0.5,
					annotation_text=f"SL: ${stop_loss:.2f}",
					annotation_position="left",
					row=1, col=1
				)
			
			# Take profit line
			if take_profit > 0:
				fig.add_hline(
					y=take_profit,
					line_dash="dot",
					line_color="green",
					opacity=0.5,
					annotation_text=f"TP: ${take_profit:.2f}",
					annotation_position="left",
					row=1, col=1
				)
		
		# Plot current price line (on the right side)
		if self.current_price > 0:
			fig.add_hline(
				y=self.current_price,
				line_dash="solid",
				line_color="blue",
				opacity=0.8,
				line_width=2,
				annotation_text=f"Current: ${self.current_price:.2f}",
				annotation_position="right",
				row=1, col=1
			)
		
		# Plot volume
		fig.add_trace(
			go.Bar(
				x=display_df.index,
				y=display_df['volume'],
				name='Volume',
				marker_color='rgba(100, 100, 100, 0.5)'
			),
			row=2, col=1
		)
		
		# Update layout
		fig.update_layout(
			title=f"{self.symbol} - Live Trading Chart | Zones: {len(self.zones)} | Entries: {len(self.entry_points)}",
			xaxis_rangeslider_visible=False,
			height=900,
			showlegend=True,
			legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
			template="plotly_dark"
		)
		
		# Update x-axis and y-axis titles
		fig.update_xaxes(title_text="Time", row=2, col=1)
		fig.update_yaxes(title_text="Price (USDT)", row=1, col=1)
		fig.update_yaxes(title_text="Volume", row=2, col=1)
		
		# Восстанавливаем сохраненное состояние зума и выделений ПОСЛЕ всех обновлений layout
		if self.saved_layout:
			# Восстанавливаем диапазоны осей для каждого subplot
			# xaxis для первого subplot (row=1), xaxis2 для второго (row=2)
			# yaxis для первого subplot (row=1), yaxis2 для второго (row=2)
			for key, value in self.saved_layout.items():
				# Проверяем, что value - это список/кортеж из двух элементов (min, max)
				if isinstance(value, (list, tuple)) and len(value) == 2 and value[0] is not None and value[1] is not None:
					if key == 'xaxis.range':
						fig.update_xaxes(range=list(value), row=1, col=1)
					elif key == 'xaxis2.range':
						fig.update_xaxes(range=list(value), row=2, col=1)
					elif key == 'yaxis.range':
						fig.update_yaxes(range=list(value), row=1, col=1)
					elif key == 'yaxis2.range':
						fig.update_yaxes(range=list(value), row=2, col=1)
		
		return fig
	
	def save_html(self, filepath: str = None):
		"""Save chart as HTML file"""
		if self.fig is None:
			self.fig = self._create_chart()
		else:
			self.fig = self._create_chart()
		
		if filepath is None:
			import os
			charts_dir = os.path.join("charts", self.symbol)
			os.makedirs(charts_dir, exist_ok=True)
			filepath = os.path.join(charts_dir, f"{self.symbol}_live_chart.html")
		
		self.fig.write_html(filepath, auto_open=False)
		return filepath
	
	def show(self, block: bool = False):
		"""Show the chart in browser"""
		if self.fig is None:
			self.fig = self._create_chart()
		else:
			self.fig = self._create_chart()
		
		self.fig.show()
		
		if block:
			# Keep the process alive
			while self.is_running:
				time.sleep(1)
	
	
	def _create_dash_app(self):
		"""Create Dash application with auto-updating chart"""
		self.app = Dash(__name__)
		
		self.app.layout = html.Div([
			html.Div([
				html.H1(f"{self.symbol} - Live Trading Chart", 
				       style={'textAlign': 'center', 'color': 'white', 'marginBottom': '20px'}),
				html.Div(id='status-info', style={'textAlign': 'center', 'color': 'lightgray', 'marginBottom': '10px'}),
				dcc.Graph(id='live-chart', style={'height': '900px'}),
				dcc.Interval(
					id='interval-component',
					interval=self.update_interval * 1000,  # in milliseconds
					n_intervals=0
				)
			], style={'backgroundColor': '#1e1e1e', 'padding': '20px'})
		], style={'backgroundColor': '#1e1e1e', 'minHeight': '100vh'})
		
		@self.app.callback(
			[Output('live-chart', 'figure'),
			 Output('status-info', 'children')],
			[Input('interval-component', 'n_intervals')],
			[State('live-chart', 'relayoutData')]
		)
		def update_chart(n, relayout_data):
			"""Update chart callback with preserved zoom and pan"""
			# Сохраняем текущее состояние зума и выделений перед обновлением
			if relayout_data:
				# Извлекаем диапазоны осей из relayoutData
				# relayoutData может содержать ключи вида 'xaxis.range[0]', 'xaxis.range[1]' или 'xaxis.range'
				saved_state = {}
				range_parts = {}  # Временное хранилище для частей диапазонов
				
				for key, value in relayout_data.items():
					if '.range' in key:
						# Обрабатываем разные форматы: 'xaxis.range[0]', 'xaxis.range[1]', 'xaxis.range'
						if '.range[0]' in key:
							axis_key = key.replace('.range[0]', '.range')
							if axis_key not in range_parts:
								range_parts[axis_key] = {'min': value, 'max': None}
							else:
								range_parts[axis_key]['min'] = value
						elif '.range[1]' in key:
							axis_key = key.replace('.range[1]', '.range')
							if axis_key not in range_parts:
								range_parts[axis_key] = {'min': None, 'max': value}
							else:
								range_parts[axis_key]['max'] = value
						elif key.endswith('.range'):
							# Если значение уже список/кортеж из двух элементов
							if isinstance(value, (list, tuple)) and len(value) == 2:
								saved_state[key] = list(value)
							else:
								saved_state[key] = value
				
				# Объединяем части диапазонов в полные диапазоны
				for axis_key, parts in range_parts.items():
					if parts['min'] is not None and parts['max'] is not None:
						saved_state[axis_key] = [parts['min'], parts['max']]
				
				# Сохраняем состояние только если есть изменения в зуме/пане
				# Исключаем случаи, когда пользователь сбросил зум (autosize и т.д.)
				if saved_state and 'autosize' not in relayout_data:
					self.saved_layout = saved_state
				elif 'autosize' in relayout_data or 'xaxis.autorange' in relayout_data or 'yaxis.autorange' in relayout_data:
					# Если пользователь сбросил зум, очищаем сохраненное состояние
					self.saved_layout = None
			
			fig = self._create_chart()
			status_text = f"Zones: {len(self.zones)} | Entries: {len(self.entry_points)} | Price: ${self.current_price:.2f} | Last update: {datetime.now().strftime('%H:%M:%S')}"
			return fig, status_text
	
	def start(self, html_filepath: str = None):
		"""Start live chart with Dash server"""
		if self.is_running:
			return
		
		self.is_running = True
		
		# Create Dash app
		self._create_dash_app()
		
		# Start Dash server in separate thread
		def run_server():
			try:
				# Use app.run() for newer Dash versions, fallback to run_server() for older versions
				if hasattr(self.app, 'run'):
					self.app.run(debug=False, host='127.0.0.1', port=self.port, use_reloader=False)
				else:
					self.app.run_server(debug=False, host='127.0.0.1', port=self.port, use_reloader=False)
			except Exception as e:
				print(f"⚠️ Error running Dash server: {e}")
		
		self.thread = threading.Thread(target=run_server, daemon=True)
		self.thread.start()
		
		# Wait a bit for server to start
		time.sleep(2)
		
		# Open browser
		import webbrowser
		url = f"http://127.0.0.1:{self.port}"
		webbrowser.open(url)
		
		print(f"📊 Live chart started at: http://127.0.0.1:{self.port}")
		print(f"   Chart will update automatically every {self.update_interval} seconds.")
		print(f"   No page refresh needed - updates happen in real-time!")
	
	def stop(self):
		"""Stop live chart"""
		self.is_running = False
		# Note: Dash server will stop when main process exits
		# For graceful shutdown, you may need to implement server.stop() if needed
		if self.thread:
			self.thread.join(timeout=2)
	
	def add_entry_point(self, entry_time: datetime, entry_price: float, 
	                    direction: str, zone_id: int, stop_loss: float = 0, 
	                    take_profit: float = 0):
		"""Add entry point to chart"""
		entry = {
			'time': entry_time,
			'price': entry_price,
			'direction': direction,
			'zone_id': zone_id,
			'stop_loss': stop_loss,
			'take_profit': take_profit
		}
		self.entry_points.append(entry)
		print(f"📊 Entry point added to chart: {direction} @ ${entry_price:.2f} (Zone {zone_id})")
	
	def remove_entry_points(self, zone_id: int = None):
		"""Remove entry points from chart. If zone_id is None, removes all entry points."""
		if zone_id is None:
			removed_count = len(self.entry_points)
			self.entry_points.clear()
			if removed_count > 0:
				print(f"📊 Removed all entry points from chart ({removed_count} total)")
		else:
			initial_count = len(self.entry_points)
			self.entry_points = [ep for ep in self.entry_points if ep.get('zone_id') != zone_id]
			removed_count = initial_count - len(self.entry_points)
			if removed_count > 0:
				print(f"📊 Removed {removed_count} entry point(s) for zone {zone_id} from chart")

