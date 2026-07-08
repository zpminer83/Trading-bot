from exchange.client import DreamDexClient


def main():

    client = DreamDexClient()

    markets = client.get_markets()

    print()

    print("Available markets")

    print("-" * 50)

    for market in markets.markets:

        print(market.symbol)

    client.close()


if __name__ == "__main__":
    main()