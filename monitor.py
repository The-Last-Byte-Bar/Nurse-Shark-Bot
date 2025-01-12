# monitor.py
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
import logging
from models import AddressInfo, Transaction, WalletBalance, TokenBalance
from clients import ExplorerClient
from services import TransactionAnalyzer, BalanceTracker
from notifications import TransactionHandler

class ErgoTransactionMonitor:
    def __init__(
        self,
        explorer_client: ExplorerClient,
        transaction_handlers: List[TransactionHandler],
        daily_report_hour: int = 12  # Add this parameter
    ):
        self.explorer_client = explorer_client
        self.transaction_handlers = transaction_handlers
        self.watched_addresses: Dict[str, AddressInfo] = {}
        self.processed_mempool_txs: Set[str] = set()
        self.processed_confirmed_txs: Set[str] = set()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.last_daily_report = None
        self.daily_report_hour = daily_report_hour  # Store it as instance variable

    async def update_balances(self):
        """Update balances for all watched addresses"""
        for address in self.watched_addresses:
            try:
                new_balance = await BalanceTracker.get_current_balance(self.explorer_client, address)
                self.watched_addresses[address].balance = new_balance
            except Exception as e:
                self.logger.error(f"Failed to update balance for {address}: {str(e)}")

    async def send_daily_balance_report(self):
        """Send daily balance report for addresses with report_balance enabled"""
        try:
            # Update all balances first
            await self.update_balances()
            
            # Filter addresses that should be included in the report
            reportable_addresses = {
                addr: info for addr, info in self.watched_addresses.items() 
                if info.report_balance
            }
            
            if not reportable_addresses:
                return  # Skip if no addresses are configured for balance reporting
            
            message = [
                "📊 *Daily Balance Report*",
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            ]
            
            # Sort addresses by nickname for consistent ordering
            sorted_addresses = sorted(
                reportable_addresses.items(),
                key=lambda x: x[1].nickname
            )
            
            for address, info in sorted_addresses:
                message.extend([
                    f"*{info.nickname}*",
                    f"ERG: `{info.balance.erg_balance:.8f}`"
                ])
                
                if info.balance.tokens:
                    sorted_tokens = sorted(
                        info.balance.tokens.values(),
                        key=lambda x: x.amount,
                        reverse=True
                    )
                    for token in sorted_tokens:
                        token_name = token.name or f"[{token.token_id[:12]}...]"
                        message.append(f"`{token.amount:>12}` {token_name}")
                message.append("")  # Add blank line between addresses
            
            # Send to all handlers
            for handler in self.transaction_handlers:
                await handler.handle_transaction(
                    address="daily_report",  # Special address for daily report
                    transaction=None,  # No transaction for daily report
                    monitor=self
                )
            
            self.logger.info("Daily balance report sent successfully")
            
        except Exception as e:
            self.logger.error(f"Error sending daily balance report: {str(e)}")

    async def check_transactions(self, address: str) -> List[Transaction]:
        """Check for new transactions for a specific address"""
        address_info = self.watched_addresses[address]
        new_transactions = []

        try:
            transactions = await self.explorer_client.get_address_transactions(address)
            current_time = datetime.now()
            
            for tx in transactions:
                tx_id = tx.get('id')
                is_mempool = tx.get('mempool', False)
                
                # Determine if we should process this transaction
                should_process = False
                
                if is_mempool:
                    if tx_id not in self.processed_mempool_txs:
                        should_process = True
                        self.processed_mempool_txs.add(tx_id)
                else:
                    if (tx_id not in self.processed_confirmed_txs or 
                        tx_id in self.processed_mempool_txs):
                        should_process = True
                        self.processed_mempool_txs.discard(tx_id)
                        self.processed_confirmed_txs.add(tx_id)
                
                if should_process:
                    tx_time = datetime.fromtimestamp(tx.get('timestamp', 0) / 1000)
                    
                    if tx_time > address_info.last_check:
                        tx_details = TransactionAnalyzer.extract_transaction_details(tx, address)
                        
                        if abs(tx_details.value) > 0.0001 or tx_details.tokens:
                            new_transactions.append(tx_details)
                            
                            # Keep processed transaction sets from growing too large
                            if len(self.processed_confirmed_txs) > 1000:
                                self.processed_confirmed_txs = set(
                                    list(self.processed_confirmed_txs)[-1000:]
                                )
                            if len(self.processed_mempool_txs) > 100:
                                self.processed_mempool_txs = set(
                                    list(self.processed_mempool_txs)[-100:]
                                )
                    else:
                        break
            
            # Update the last check time if we successfully processed transactions
            if new_transactions or not transactions:
                self.watched_addresses[address] = AddressInfo(
                    address=address_info.address,
                    nickname=address_info.nickname,
                    last_check=current_time,
                    last_height=max(
                        [tx.get('height', 0) for tx in transactions[:1]] 
                        or [address_info.last_height]
                    ),
                    balance=address_info.balance  # Preserve the balance
                )
            
        except Exception as e:
            self.logger.error(f"Error checking transactions for {address}: {str(e)}")
        
        return new_transactions

    async def monitor_loop(self, check_interval: int = 60):
        """Main monitoring loop"""
        self.logger.info("Starting monitoring loop...")
        
        try:
            while True:
                current_time = datetime.now()
                
                # Check if we need to send daily report
                if (self.last_daily_report is None or 
                    current_time.date() > self.last_daily_report.date()):
                    if current_time.hour == self.daily_report_hour:
                        await self.send_daily_balance_report()
                        self.last_daily_report = current_time
                
                # Update balances first
                await self.update_balances()
                
                for address in list(self.watched_addresses.keys()):
                    try:
                        transactions = await self.check_transactions(address)
                        
                        if transactions:
                            for tx in sorted(transactions, key=lambda x: x.timestamp):
                                for handler in self.transaction_handlers:
                                    await handler.handle_transaction(address, tx, self)
                                    
                    except Exception as e:
                        self.logger.error(f"Error processing address {address}: {str(e)}")
                
                await asyncio.sleep(check_interval)
        finally:
            await self.explorer_client.close_session()
            
    def add_address(
        self, 
        address: str, 
        nickname: Optional[str] = None, 
        hours_lookback: int = 1, 
        report_balance: bool = True
    ):
        """Add address with optional balance reporting configuration"""
        if not address or len(address) < 40:
            raise ValueError(f"Invalid Ergo address format: {address}")
        
        lookback_time = datetime.now() - timedelta(hours=hours_lookback)
        lookback_time = lookback_time.replace(minute=0, second=0, microsecond=0)
        
        self.watched_addresses[address] = AddressInfo(
            address=address,
            nickname=nickname or address[:8],
            last_check=lookback_time,
            last_height=0,
            report_balance=report_balance
        )
        
        self.logger.info(
            f"Added address {nickname or address[:8]} to monitoring list "
            f"with {hours_lookback}h lookback from {lookback_time}"
        )