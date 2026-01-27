"""
Health check utilities for ArcTrigger
Monitors thread health, connection status, and system state
"""
import threading
import logging
import time
from typing import Dict, List, Optional
from Services.runtime_manager import runtime_man


class HealthChecker:
    """
    Monitors application health and reports issues.
    """
    
    def __init__(self):
        self._critical_threads = set()
        self._last_check = 0
        self._check_interval = 30  # seconds
        self._issues = []
    
    def register_critical_thread(self, thread_name: str):
        """Register a thread that must be alive for system to function."""
        self._critical_threads.add(thread_name)
        logging.info(f"[HealthCheck] Registered critical thread: {thread_name}")
    
    def check_thread_health(self) -> List[str]:
        """
        Check if all critical threads are alive.
        
        Returns:
            List of issues found (empty if healthy)
        """
        issues = []
        alive_threads = {t.name for t in threading.enumerate()}
        
        for thread_name in self._critical_threads:
            if thread_name not in alive_threads:
                issues.append(f"Critical thread '{thread_name}' is not running")
                logging.error(f"[HealthCheck] ❌ Thread '{thread_name}' is dead!")
        
        return issues
    
    def check_runtime_state(self) -> List[str]:
        """
        Check runtime manager state.
        
        Returns:
            List of issues found
        """
        issues = []
        
        if not runtime_man.is_run():
            issues.append("Runtime manager is stopped")
            logging.warning("[HealthCheck] Runtime manager is stopped")
        
        return issues
    
    def get_thread_count(self) -> Dict[str, int]:
        """Get statistics about running threads."""
        threads = threading.enumerate()
        
        daemon_count = sum(1 for t in threads if t.daemon)
        non_daemon_count = len(threads) - daemon_count
        
        return {
            "total": len(threads),
            "daemon": daemon_count,
            "non_daemon": non_daemon_count,
            "threads": [t.name for t in threads]
        }
    
    def run_health_check(self) -> Dict:
        """
        Run comprehensive health check.
        
        Returns:
            Dictionary with health status and issues
        """
        issues = []
        
        # Check threads
        thread_issues = self.check_thread_health()
        issues.extend(thread_issues)
        
        # Check runtime
        runtime_issues = self.check_runtime_state()
        issues.extend(runtime_issues)
        
        # Get thread stats
        thread_stats = self.get_thread_count()
        
        status = {
            "healthy": len(issues) == 0,
            "issues": issues,
            "thread_stats": thread_stats,
            "timestamp": time.time()
        }
        
        if issues:
            logging.warning(f"[HealthCheck] Found {len(issues)} issue(s): {issues}")
        else:
            logging.debug("[HealthCheck] ✅ System healthy")
        
        self._last_check = time.time()
        self._issues = issues
        
        return status
    
    def start_monitoring(self, interval: int = 60):
        """
        Start background health monitoring thread.
        
        Args:
            interval: Check interval in seconds
        """
        def monitor_loop():
            while runtime_man.is_run():
                try:
                    self.run_health_check()
                except Exception as e:
                    logging.error(f"[HealthCheck] Monitor error: {e}")
                finally:
                    time.sleep(interval)
        
        monitor_thread = threading.Thread(
            target=monitor_loop,
            name="HealthMonitor",
            daemon=True
        )
        monitor_thread.start()
        logging.info(f"[HealthCheck] Health monitoring started (interval: {interval}s)")


# Global instance
health_checker = HealthChecker()


def register_critical_threads():
    """Register all critical threads that should be monitored."""
    # Add thread names that are critical for operation
    health_checker.register_critical_thread("OrderWaitService")
    health_checker.register_critical_thread("OrderFixerService")
    # Note: Some threads may have different names - adjust as needed


def get_health_status() -> Dict:
    """Quick health check - returns current status."""
    return health_checker.run_health_check()
