"""
Unit tests for ConnectionManager.

Tests connection lifecycle, reconnection logic, and subscription restoration.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from ib_insync import IB

from src.data.ibkr.connection_manager import ConnectionManager


class TestConnectionManager:
    """Test suite for ConnectionManager."""
    
    @pytest.fixture
    def connection_manager(self):
        """Create ConnectionManager instance for testing."""
        return ConnectionManager(
            host="127.0.0.1",
            port=7497,
            client_id=2
        )
    
    def test_initialization(self, connection_manager):
        """Test ConnectionManager initialization."""
        assert connection_manager.host == "127.0.0.1"
        assert connection_manager.port == 7497
        assert connection_manager.client_id == 2
        assert connection_manager._ib is None
        assert connection_manager._is_connected is False
        assert len(connection_manager._disconnect_handlers) == 0
    
    @pytest.mark.asyncio
    async def test_connect_success(self, connection_manager):
        """Test successful connection to IBKR."""
        # Mock IB instance
        mock_ib = MagicMock(spec=IB)
        mock_ib.connectAsync = AsyncMock(return_value=None)
        mock_ib.isConnected = MagicMock(return_value=True)
        mock_ib.disconnectedEvent = MagicMock()
        
        with patch('src.data.ibkr.connection_manager.IB', return_value=mock_ib):
            success = await connection_manager.connect()
        
        assert success is True
        assert connection_manager.is_connected() is True
        mock_ib.connectAsync.assert_called_once_with(
            host="127.0.0.1",
            port=7497,
            clientId=2,
            timeout=20
        )
    
    @pytest.mark.asyncio
    async def test_connect_failure(self, connection_manager):
        """Test connection failure handling."""
        # Mock IB instance that raises exception
        mock_ib = MagicMock(spec=IB)
        mock_ib.connectAsync = AsyncMock(side_effect=Exception("Connection failed"))
        
        with patch('src.data.ibkr.connection_manager.IB', return_value=mock_ib):
            success = await connection_manager.connect()
        
        assert success is False
        assert connection_manager.is_connected() is False
    
    @pytest.mark.asyncio
    async def test_reconnect_exponential_backoff(self, connection_manager):
        """Test reconnection with exponential backoff timing."""
        # Mock connect to fail 3 times then succeed
        call_count = 0
        
        async def mock_connect():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return False
            return True
        
        connection_manager.connect = mock_connect
        
        # Track sleep calls to verify exponential backoff
        sleep_calls = []
        
        async def mock_sleep(delay):
            sleep_calls.append(delay)
        
        with patch('asyncio.sleep', side_effect=mock_sleep):
            success = await connection_manager.reconnect()
        
        assert success is True
        assert call_count == 3
        # Verify exponential backoff: 2^1=2, 2^2=4
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == 2.0
        assert sleep_calls[1] == 4.0
    
    @pytest.mark.asyncio
    async def test_reconnect_max_attempts(self, connection_manager):
        """Test reconnection fails after max attempts."""
        # Mock connect to always fail
        connection_manager.connect = AsyncMock(return_value=False)
        
        with patch('asyncio.sleep', new_callable=AsyncMock):
            success = await connection_manager.reconnect()
        
        assert success is False
        # Should attempt 5 times
        assert connection_manager.connect.call_count == 5
    
    def test_is_connected(self, connection_manager):
        """Test connection status check."""
        # Initially not connected
        assert connection_manager.is_connected() is False
        
        # Mock connected state
        mock_ib = MagicMock(spec=IB)
        mock_ib.isConnected = MagicMock(return_value=True)
        connection_manager._ib = mock_ib
        connection_manager._is_connected = True
        
        assert connection_manager.is_connected() is True
    
    def test_get_ib_instance_not_connected(self, connection_manager):
        """Test get_ib_instance raises error when not connected."""
        with pytest.raises(RuntimeError, match="Not connected to IBKR"):
            connection_manager.get_ib_instance()
    
    def test_get_ib_instance_connected(self, connection_manager):
        """Test get_ib_instance returns IB instance when connected."""
        # Mock connected state
        mock_ib = MagicMock(spec=IB)
        mock_ib.isConnected = MagicMock(return_value=True)
        connection_manager._ib = mock_ib
        connection_manager._is_connected = True
        
        ib = connection_manager.get_ib_instance()
        assert ib is mock_ib
    
    def test_register_disconnect_handler(self, connection_manager):
        """Test registering disconnect handlers."""
        handler1 = MagicMock()
        handler2 = MagicMock()
        
        connection_manager.register_disconnect_handler(handler1)
        connection_manager.register_disconnect_handler(handler2)
        
        assert len(connection_manager._disconnect_handlers) == 2
        assert handler1 in connection_manager._disconnect_handlers
        assert handler2 in connection_manager._disconnect_handlers
    
    @pytest.mark.asyncio
    async def test_notify_disconnect_handlers(self, connection_manager):
        """Test disconnect handlers are notified."""
        # Register async and sync handlers
        async_handler = AsyncMock()
        sync_handler = MagicMock()
        
        connection_manager.register_disconnect_handler(async_handler)
        connection_manager.register_disconnect_handler(sync_handler)
        
        await connection_manager._notify_disconnect_handlers()
        
        async_handler.assert_called_once()
        sync_handler.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_notify_disconnect_handlers_error_handling(self, connection_manager):
        """Test disconnect handler errors are caught and logged."""
        # Register handler that raises exception
        failing_handler = AsyncMock(side_effect=Exception("Handler failed"))
        working_handler = AsyncMock()
        
        connection_manager.register_disconnect_handler(failing_handler)
        connection_manager.register_disconnect_handler(working_handler)
        
        # Should not raise exception
        await connection_manager._notify_disconnect_handlers()
        
        # Working handler should still be called
        working_handler.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_disconnect(self, connection_manager):
        """Test graceful disconnect."""
        # Mock connected state
        mock_ib = MagicMock(spec=IB)
        mock_ib.isConnected = MagicMock(return_value=True)
        mock_ib.disconnect = MagicMock()
        connection_manager._ib = mock_ib
        connection_manager._is_connected = True
        
        await connection_manager.disconnect()
        
        mock_ib.disconnect.assert_called_once()
        assert connection_manager._is_connected is False
    
    @pytest.mark.asyncio
    async def test_on_disconnect_triggers_reconnect(self, connection_manager):
        """Test disconnect event triggers reconnection."""
        # Mock reconnect
        connection_manager.reconnect = AsyncMock(return_value=True)
        
        await connection_manager._on_disconnect()
        
        assert connection_manager._is_connected is False
        connection_manager.reconnect.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
