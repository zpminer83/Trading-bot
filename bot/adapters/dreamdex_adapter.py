class DreamDexAdapter:
    """
    Адаптер между нашим ботом и официальным dreamdex-bot-kit.
    """

    def __init__(self):
        self.connected = False

    def connect(self):
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def get_markets(self):
        raise NotImplementedError

    def place_order(self, *args, **kwargs):
        raise NotImplementedError

    def cancel_order(self, order_id):
        raise NotImplementedError