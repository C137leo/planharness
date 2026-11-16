"""Reviewed capabilities gated by the actual MCP tool discovery for this account."""
from app.agent.capabilities import Capability, NoArgs
from app.food.commerce_contract import SearchStores, PrepareOrder, ReviseOrder, CheckoutVersion, SubmitOrder, CancelExistingOrder, StoreSelection

SPECS=[
 ('commerce.nutrition','查询麦当劳官方营养表，可结合档案和今日饮食提出建议；不是门店供应或价格，不能保证过敏安全。',NoArgs,('commerce_account',),False,'never',{'list-nutrition-foods'}),
 ('commerce.search_stores','查询麦当劳到店自取门店，使用本轮明确城市和地标，缺少时可引用当前用户saved_location；没有可靠城市先澄清，不猜默认城市。用户要求最近餐厅时selection_policy=nearest，指定某餐厅specific，只浏览browse。最近店关闭时必须确认改用最近营业店。距离仅为接口结果，不能冒充用户GPS定位。',SearchStores,('commerce_account',),False,'never',{'query-nearby-stores'}),
 ('commerce.select_store','确认从本次实际搜索结果改用另一家营业门店；search_revision复制store_search.revision。确认只选门店，不授权提交订单；用户已明确指定该营业门店时无需重复询问，否则必须等待改店确认；之后继续原点餐目标。',StoreSelection,('commerce_store',),True,'uncertain',{'query-nearby-stores'}),
 ('commerce.menu','读取所选真实门店当前菜单及价格；选择商品前必须查询。',NoArgs,('commerce_store',),False,'never',{'query-meals'}),
 ('commerce.product_detail','读取已绑定SKU的详情，区分套餐组与顶层特制组；无额外选项仍是有效商品，不能据此说用户没选大小/冷热。返回真实选项ID及当前商品信息。',NoArgs,('commerce_product',),False,'never',{'query-meal-detail'}),
 ('commerce.prepare_order','为所选门店准备真实报价草案，不提交订单。items必须用该店的product_id；套餐需选择选项ID或明确使用官方默认组合。未指定取餐方式时留空，系统从真实选项默认外带；明确堂食时take_way_preference=dine_in，无需先知道官方code。',PrepareOrder,('commerce_store',),True,'never',{'query-meals','query-meal-detail','calculate-price'}),
 ('commerce.revise_order','修改指定版本订单草案并重新报价，不能修改已提交或未知的订单；不提交订单。',ReviseOrder,('checkout',),True,'never',{'query-meals','query-meal-detail','calculate-price'}),
 ('commerce.submit_order','仅提交用户已看过的准确版本报价，创建到店自取待支付订单，不代付。未经完整预览不能直接提交；价格变化必须重新确认。已有有效未支付旧单时先展示选择，用户明确保留旧单另下一单时复制快照order_conflict.fingerprint到existing_order_decision，不能自行生成或省略。',SubmitOrder,('checkout',),True,'always',{'create-order','query-order','calculate-price','query-meals','query-meal-detail','query-nearby-stores'}),
 ('commerce.cancel_order','单独取消已有未支付未过期旧单，目标绑定当前新checkout预览，existing_checkout_id和decision_fingerprint复制该预览order_conflict。取消后只恢复新预览，不自动下单；已支付单不退款。',CancelExistingOrder,('checkout',),True,'always',{'cancel-order','query-order'}),
 ('commerce.review_existing_orders','刷新当前新预览对应的旧订单状态并显示支付链接和选择，不下单不取消。已有order_conflict时用户要继续支付/查询旧单用此能力。',CheckoutVersion,('checkout',),False,'never',{'query-order'}),
 ('commerce.query_order','查询本人草案关联的真实订单或未知提交结果，绝不重新下单。',NoArgs,('checkout',),False,'never',{'query-order'}),
 ('commerce.cancel_checkout','取消尚未提交的草案，不是取消远程已存在订单。',CheckoutVersion,('checkout',),True,'never',set()),
]
CAPABILITIES={id:Capability(id,'commerce',description,args,types,mutates,confirmation,'commerce',
    execution_boundary='commerce',idempotency_policy='checkout_business_key_write_ahead_claim',
    verification_policy='fresh_quote_and_authoritative_order_query') for id,description,args,types,mutates,confirmation,tools in SPECS}
REQUIRED_TOOLS={row[0]:row[-1] for row in SPECS}


def available(tools):
    return [cap for id,cap in CAPABILITIES.items() if REQUIRED_TOOLS[id] <= set(tools)]
