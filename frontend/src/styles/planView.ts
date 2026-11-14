import type { AgentDay, CurrentPlan, PlanAction } from '../api/types'
import { actionLabel, slotLabel } from './copy'

// PlanAction is a mixed table. Only its explicit discriminator determines type.
export function trainingActions(actions: PlanAction[]): PlanAction[] {
  return actions.filter(action => action.action_type === 'exercise')
}

export function weeklyMeals(plan: CurrentPlan) {
  const slot = (value: string) => value.replace(/^meal_/, '')
  const days = new Set([...plan.meals.map(meal => meal.day_index),
    ...plan.actions.filter(action => action.action_type === 'meal').map(action => action.day_index)])
  return [...days].sort((a, b) => a - b).map(day => {
    const meals = plan.meals.filter(meal => meal.day_index === day)
    const occupied = new Set(meals.map(meal => slot(meal.slot)))
    const legacy = plan.actions.filter(action => action.day_index === day && action.action_type === 'meal' && !occupied.has(slot(action.slot)))
    return { day, meals, legacy }
  })
}

export function datedView(day: AgentDay) {
  return {
    actions: day.actions,
    meals: day.meals,
    adjustment: day.adjustment || null,
    totalMinutes: day.total_minutes,
    countLabel: day.actions.length ? `${day.actions.length} 项` : '休',
  }
}

export function legacyMealTitle(action: PlanAction): string {
  return `${slotLabel(action.slot)} · ${actionLabel(action)}`
}
