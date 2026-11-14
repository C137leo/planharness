import type { AgentCheckout } from '../api/types'

// Preserve actual official option choices while changing only quantities.
export function selectedCart(quantities: Record<string,number>, cart: AgentCheckout['cart'] = []) {
  return Object.entries(quantities).filter(([,quantity]) => quantity>0).map(([product_id,quantity]) => {
    const previous=cart.find(item=>item.product_id===product_id)?.selection
    return {...(previous || {product_id,use_default_options:true}),quantity}
  })
}
