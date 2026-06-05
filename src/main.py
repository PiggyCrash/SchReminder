import os
import sys
import logging

# Ensure the project root is in the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runner import run_scout_pipeline

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MainEntry")

def main():
    """
    Main entry point. Invokes the orchestrator runner.
    """
    try:
        success = run_scout_pipeline()
        if not success:
            logger.error("Daily Academic Scout pipeline completed with critical errors.")
            sys.exit(1)
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Daily Academic Scout failed catastrophically: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
