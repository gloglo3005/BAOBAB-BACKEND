from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Consultation seule : le journal ne se crée ni ne se corrige à la main."""

    list_display = (
        "created_at", "organization", "action", "user",
        "entity_type", "entity_id", "description",
    )
    list_filter = ("organization", "action")
    search_fields = ("description", "entity_id", "user__email")
    date_hierarchy = "created_at"
    list_select_related = ("organization", "user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
