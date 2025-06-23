from uuid import uuid4
from ee.hogai.graph.base import AssistantNode
from ee.hogai.graph.billing.prompts import BILLING_CONTEXT_PROMPT
from ee.hogai.utils.types import AssistantState, PartialAssistantState
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig

from posthog.schema import AssistantToolCallMessage, MaxBillingContext


class BillingNode(AssistantNode):
    def run(self, state: AssistantState, config: RunnableConfig) -> PartialAssistantState:
        billing_context = self._get_billing_context(config)
        if not billing_context:
            return PartialAssistantState(
                messages=[
                    AssistantToolCallMessage(
                        content="No billing information available", id=str(uuid4()), tool_call_id=str(uuid4())
                    )
                ]
            )
        formatted_billing_context = self._format_billing_context(billing_context)
        tool_call_id = state.root_tool_call_id
        return PartialAssistantState(
            messages=[
                AssistantToolCallMessage(content=formatted_billing_context, tool_call_id=tool_call_id, id=str(uuid4())),
            ]
        )

    def _format_billing_context(self, billing_context: MaxBillingContext) -> str:
        """Format billing context into a readable prompt section."""
        # Convert billing context to a format suitable for the mustache template
        template_data = {
            "subscription_level": billing_context.subscription_level.value
            if billing_context.subscription_level
            else "free",
            "billing_plan": billing_context.billing_plan,
            "has_active_subscription": billing_context.has_active_subscription,
            "is_deactivated": billing_context.is_deactivated,
        }

        # Add startup program info
        if billing_context.startup_program_label:
            template_data["startup_program_label"] = billing_context.startup_program_label

        # Add billing period info
        if billing_context.billing_period:
            template_data["billing_period"] = {
                "current_period_start": billing_context.billing_period.current_period_start,
                "current_period_end": billing_context.billing_period.current_period_end,
                "interval": billing_context.billing_period.interval,
            }

        # Add cost information
        if billing_context.total_current_amount_usd:
            template_data["total_current_amount_usd"] = billing_context.total_current_amount_usd
        if billing_context.total_projected_amount_usd:
            template_data["total_projected_amount_usd"] = billing_context.total_projected_amount_usd

        # Add products information
        if billing_context.products:
            template_data["products"] = []
            for product in billing_context.products:
                product_data = {
                    "name": product.name,
                    "type": product.type,
                    "description": product.description,
                    "current_usage": int(product.current_usage) if product.current_usage else None,
                    "free_usage_limit": int(product.free_usage_limit) if product.free_usage_limit else None,
                    "percentage_usage": product.percentage_usage,
                    "has_exceeded_limit": product.has_exceeded_limit,
                    "custom_limit_usd": product.custom_limit_usd,
                    "next_period_custom_limit_usd": product.next_period_custom_limit_usd,
                    "docs_url": product.docs_url,
                }
                template_data["products"].append(product_data)

        # Add addons information
        if billing_context.addons:
            template_data["addons"] = []
            for addon in billing_context.addons:
                addon_data = {
                    "name": addon.name,
                    "type": addon.type,
                    "description": addon.description,
                    "current_usage": int(addon.current_usage) if addon.current_usage else None,
                    "usage_limit": int(addon.usage_limit) if addon.usage_limit else None,
                    "subscribed": addon.subscribed,
                    "docs_url": addon.docs_url,
                }
                template_data["addons"].append(addon_data)

        # Add trial information
        if billing_context.trial:
            template_data["trial"] = {
                "is_active": billing_context.trial.is_active,
                "expires_at": billing_context.trial.expires_at,
                "target": billing_context.trial.target,
            }

        if billing_context.usage_history:
            # Format usage history as a table with breakdown by date
            usage_table = self._format_usage_history_table(billing_context.usage_history)
            template_data["usage_history_table"] = usage_table

        # Add settings
        template_data["settings"] = {"autocapture_on": billing_context.settings.autocapture_on}

        template = PromptTemplate.from_template(BILLING_CONTEXT_PROMPT, template_format="mustache")
        return template.format_prompt(**template_data).to_string()

    def _format_usage_history_table(self, usage_history) -> str:
        """Format usage history timeseries data as a readable table."""
        if not usage_history:
            return ""

        # Create table header
        table_lines = ["| Date | Usage |", "|------|-------|"]

        # Process each result in the usage history
        for result in usage_history:
            dates = result.get("dates", [])
            data = result.get("data", [])
            breakdown_value = result.get("breakdown_value", "")

            # If there's a breakdown value, include it in the header
            if breakdown_value and len(usage_history) > 1:
                table_lines.append(f"| **{breakdown_value}** | |")

            # Add data rows
            for date, usage in zip(dates, data):
                table_lines.append(f"| {date} | {usage:,} |")

        return "\n".join(table_lines)
