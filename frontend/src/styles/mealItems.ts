/** Display grouping only. Original item IDs remain available to the Agent. */
export function displayMealItems<T extends { name: string; grams: number }>(items: T[]): T[] {
  const result: T[] = []
  const identityFields = ['ingredient_id', 'ingredient_key'] as const
  const metadata = ['preparation', 'cooking_method', 'cooked_state', 'brand', 'unit', 'meta']
  for (const item of items) {
    const row = item as T & Record<string, unknown>
    const existing = result.find((candidate) => {
      const other = candidate as T & Record<string, unknown>
      return !!row.name && other.name === row.name &&
        identityFields.every((key) => !row[key] || !other[key] || row[key] === other[key]) &&
        metadata.every((key) => JSON.stringify(row[key]) === JSON.stringify(other[key]))
    })
    if (existing) {
      existing.grams = Math.round((Number(existing.grams) + Number(item.grams)) * 10000) / 10000
      const merged = existing as unknown as Record<string, unknown>
      for (const key of identityFields) {
        if (!merged[key] && row[key]) merged[key] = row[key]
      }
    } else result.push({ ...item })
  }
  return result
}
