#!/usr/bin/env python3
"""
Replay Test Tool - Standalone script to test historical data replay
Completely isolated - doesn't modify existing application code

Usage:
    python replay_test.py --date 2026-01-22 --symbol TSLA
    python replay_test.py --date 2026-01-22 --symbol TSLA --speed 2.0
    python replay_test.py --date 2026-01-22 --symbol TSLA --order-mode simulate
"""
import argparse
import logging
import sys
import time
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Import replay components
from Replay.replay_service import ReplayService
from Replay.historical_data_service import HistoricalDataService
from Replay.tws_mock_service import TWSMockService

# Import existing services (for testing)
try:
    from Services.polygon_service import polygon_service
    from model import general_app
    from Services.order_wait_service import OrderWaitService
    from Services.tws_service import create_tws_service
except ImportError as e:
    logging.error(f"Failed to import services: {e}")
    logging.error("Make sure you're running from the project root directory")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Replay historical market data for testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Replay TSLA on Jan 22, 2026 at real-time speed
  python replay_test.py --date 2026-01-22 --symbol TSLA
  
  # Replay at 2x speed
  python replay_test.py --date 2026-01-22 --symbol TSLA --speed 2.0
  
  # Replay with simulated orders (no real TWS)
  python replay_test.py --date 2026-01-22 --symbol TSLA --order-mode simulate
  
  # Replay multiple symbols
  python replay_test.py --date 2026-01-22 --symbol TSLA AAPL --speed 1.0
        """
    )
    
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="Date to replay in YYYY-MM-DD format (e.g., 2026-01-22)"
    )
    
    parser.add_argument(
        "--symbol",
        type=str,
        nargs="+",
        required=True,
        help="Stock symbol(s) to replay (e.g., TSLA or TSLA AAPL)"
    )
    
    parser.add_argument(
        "--start-time",
        type=str,
        default="04:00:00",
        help="Start time in HH:MM:SS format (ET) (default: 04:00:00)"
    )
    
    parser.add_argument(
        "--end-time",
        type=str,
        default="20:00:00",
        help="End time in HH:MM:SS format (ET) (default: 20:00:00)"
    )
    
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Replay speed multiplier (1.0 = real-time, 2.0 = 2x speed) (default: 1.0)"
    )
    
    parser.add_argument(
        "--order-mode",
        type=str,
        choices=["simulate", "real"],
        default="simulate",
        help="Order placement mode: 'simulate' (mock TWS) or 'real' (paper trading) (default: simulate)"
    )
    
    args = parser.parse_args()
    
    # Validate date format
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        logging.error(f"Invalid date format: {args.date}. Use YYYY-MM-DD")
        sys.exit(1)
    
    # Initialize services
    logging.info("=" * 80)
    logging.info("🎬 Historical Data Replay Test Tool")
    logging.info("=" * 80)
    logging.info(f"Date: {args.date}")
    logging.info(f"Symbols: {', '.join(args.symbol)}")
    logging.info(f"Time range: {args.start_time} - {args.end_time} ET")
    logging.info(f"Speed: {args.speed}x")
    logging.info(f"Order mode: {args.order_mode}")
    logging.info("=" * 80)
    
    # Create replay service
    replay_service = ReplayService()
    
    # Setup order mode
    if args.order_mode == "simulate":
        logging.info("📝 Using simulated order placement (no real TWS)")
        # You can inject mock TWS service here if needed
        # For now, orders will just be logged
    else:
        logging.warning("⚠️  Using REAL order placement - make sure you're using paper trading account!")
        response = input("Continue with real orders? (yes/no): ")
        if response.lower() != "yes":
            logging.info("Aborted.")
            sys.exit(0)
    
    try:
        # Start replay
        replay_service.start_replay(
            date=args.date,
            symbols=args.symbol,
            start_time=args.start_time,
            end_time=args.end_time,
            speed=args.speed,
            order_mode=args.order_mode
        )
        
        # Wait for replay to complete
        logging.info("⏳ Replay in progress... (Press Ctrl+C to stop)")
        
        while replay_service.is_replaying:
            status = replay_service.get_replay_status()
            if status["current_time"]:
                logging.info(
                    f"⏱️  Current time: {status['current_time']} | "
                    f"Paused: {status['is_paused']} | Speed: {status['speed']}x"
                )
            time.sleep(5)  # Status update every 5 seconds
        
        logging.info("✅ Replay completed successfully!")
        
    except KeyboardInterrupt:
        logging.info("\n🛑 Replay interrupted by user")
        replay_service.stop_replay()
    except Exception as e:
        logging.error(f"❌ Replay failed: {e}", exc_info=True)
        replay_service.stop_replay()
        sys.exit(1)
    finally:
        replay_service.stop_replay()
        logging.info("=" * 80)
        logging.info("🏁 Replay test finished")
        logging.info("=" * 80)


if __name__ == "__main__":
    main()
