from django.contrib import admin

from .models import ImportBatch


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    """Consultation seule : un import se fait depuis l'interface Staff."""

    list_display = (
        "created_at", "organization", "file_name", "status",
        "row_count", "error_count", "user",
    )
    list_filter = ("organization", "status", "kind")
    search_fields = ("file_name", "user__email")
    list_select_related = ("organization", "user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
