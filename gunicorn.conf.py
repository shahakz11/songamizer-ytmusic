import logging
from server import init_mongodb

logger = logging.getLogger(__name__)

def worker_init(worker):
    logger.info(f"Initializing MongoDB for worker {worker.pid}")
    init_mongodb()