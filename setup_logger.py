import logging

def setup_logging() -> logging.Logger:
    logger = logging.getLogger('DishwasherApp')
    logger.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    file_handler = logging.FileHandler('dishwasher.log')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger