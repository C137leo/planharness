"""Reviewed read-only commerce methods. No raw user text or arbitrary tool dispatch."""
from typing import Literal
from pydantic import Field
from app.agent.binding_contract import StrictModel


class StoreSearch(StrictModel):
    city: str = Field(min_length=1, max_length=80)
    keyword: str = Field(min_length=1, max_length=120)
    be_type: Literal[1, 5]


class PickupMenu(StrictModel):
    store_code: str = Field(min_length=1, max_length=32)
    # Only reviewed pickup scope. Delivery/address/coupon writes are not exposed.


class ProductDetail(PickupMenu):
    product_code: str = Field(min_length=1, max_length=64)


class CommerceReadAdapter:
    def __init__(self, transport):
        self.transport = transport

    def stores(self, args: StoreSearch):
        return self.transport.call_read("query-nearby-stores", {
            "city": args.city, "keyword": args.keyword, "beType": args.be_type, "searchType": 2})

    def menu(self, args: PickupMenu):
        return self.transport.call_read("query-meals", {"storeCode": args.store_code, "orderType": 1, "beType": 1})

    def product(self, args: ProductDetail):
        return self.transport.call_read("query-meal-detail", {
            "storeCode": args.store_code, "code": args.product_code, "orderType": 1, "beType": 1})

    def nutrition(self):
        return self.transport.call_read("list-nutrition-foods", {})
