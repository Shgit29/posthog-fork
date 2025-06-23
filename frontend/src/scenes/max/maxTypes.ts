import { BillingUsageResponse } from 'scenes/billing/billingUsageLogic'

import { DashboardFilter, HogQLVariable, QuerySchema } from '~/queries/schema/schema-general'
import { integer } from '~/queries/schema/type-utils'

// Simplified product information for Max context
export interface MaxProductInfo {
    type: string
    name: string
    description: string
    is_used: boolean // current_usage > 0
    has_exceeded_limit: boolean
    current_usage?: number
    free_usage_limit?: number | null
    percentage_usage: number
    custom_limit_usd?: number | null
    next_period_custom_limit_usd?: number | null
    docs_url?: string
}

// Simplified addon information for Max context
export interface MaxAddonInfo {
    type: string
    name: string
    description: string
    is_used: boolean // current_usage > 0
    subscribed: boolean
    has_exceeded_limit: boolean
    current_usage: number
    usage_limit?: number | null
    percentage_usage?: number
    custom_limit_usd?: number | null
    next_period_custom_limit_usd?: number | null
    docs_url?: string
}

// Usage data context for Max
export interface MaxUsageContext {
    date_range: {
        start_date: string
        end_date: string
    }
    usage_summary: Array<{
        product_type: string
        product_name: string
        total_usage: number
        dates: string[]
        data: number[]
    }>
}

export interface MaxBillingContext {
    // Overall billing status
    has_active_subscription: boolean
    subscription_level: 'free' | 'paid' | 'custom'
    billing_plan: string | null
    is_deactivated?: boolean

    // Products information
    products: MaxProductInfo[]

    // Addons information (flattened from all products)
    addons: MaxAddonInfo[]

    // Usage summary
    total_current_amount_usd?: string
    total_projected_amount_usd?: string

    // Startup program
    startup_program_label?: string
    startup_program_label_previous?: string

    // Trial information
    trial?: {
        is_active: boolean
        expires_at?: string
        target?: string
    }

    // Billing period
    billing_period?: {
        current_period_start: string
        current_period_end: string
        interval: 'month' | 'year'
    }

    // Usage history
    usage_history?: BillingUsageResponse['results']

    // Settings
    settings: {
        autocapture_on: boolean
    }
}

export interface MaxInsightContext {
    id: string | integer
    name?: string
    description?: string

    query: QuerySchema // The actual query node, e.g., TrendsQuery, HogQLQuery
}

export interface MaxDashboardContext {
    id: string | integer
    name?: string
    description?: string
    insights: MaxInsightContext[]
    filters: DashboardFilter
}

// The main shape for the UI context sent to the backend
export interface MaxContextShape {
    dashboards?: MaxDashboardContext[]
    insights?: MaxInsightContext[]
    filters_override?: DashboardFilter
    variables_override?: Record<string, HogQLVariable>
    billing?: MaxBillingContext
}

// Taxonomic filter options
export interface MaxContextOption {
    id: string
    value: string | integer
    name: string
    icon: React.ReactNode
    type?: 'dashboard' | 'insight'
    items?: {
        insights?: MaxInsightContext[]
        dashboards?: MaxDashboardContext[]
    }
}
