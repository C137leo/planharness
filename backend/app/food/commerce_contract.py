"""Model arguments contain database IDs, never invented provider identifiers."""
from pydantic import Field
from typing import Literal
from app.agent.binding_contract import StrictModel


class SearchStores(StrictModel):
    city: str = Field(min_length=1, max_length=80)
    keyword: str = Field(min_length=1, max_length=120)
    selection_policy: Literal['browse','nearest','specific'] = 'browse'


class OptionSelection(StrictModel):
    option_id: str
    quantity: int = Field(ge=1, le=20)


class CartLine(StrictModel):
    product_id: str
    quantity: int = Field(ge=1, le=20)
    selections: list[OptionSelection] = Field(default_factory=list, max_length=30)
    use_default_options: bool = False


class PrepareOrder(StrictModel):
    items: list[CartLine] = Field(min_length=1, max_length=20)
    take_way_code: str = Field(default="", max_length=64)
    take_way_preference: Literal['takeaway','dine_in'] | None = None


class ReviseOrder(StrictModel):
    version: int = Field(ge=1)
    items: list[CartLine] | None = Field(default=None, min_length=1, max_length=20)
    take_way_code: str | None = Field(default=None, max_length=64)
    take_way_preference: Literal['takeaway','dine_in'] | None = None


class CheckoutVersion(StrictModel):
    version: int = Field(ge=1)


class SubmitOrder(CheckoutVersion):
    existing_order_decision: str = Field(default='',max_length=64)


class CancelExistingOrder(CheckoutVersion):
    existing_checkout_id: str = Field(min_length=1,max_length=64)
    decision_fingerprint: str = Field(min_length=64,max_length=64)


class StoreSelection(StrictModel):
    search_revision: str = Field(min_length=64,max_length=64)
