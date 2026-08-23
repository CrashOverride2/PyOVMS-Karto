class IPBannedException(Exception):
    """Custom exception raised when a request is from a banned IP address."""
    def __init__(self, ip_address: str):
        self.ip_address = ip_address
        super().__init__(f"IP address {ip_address} is temporarily banned.")