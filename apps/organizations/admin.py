from django.contrib import admin, messages
from django.utils import timezone

from .models import Organization, OrganizationStaff, LoanPolicy


class LoanPolicyInline(admin.StackedInline):
    model = LoanPolicy
    can_delete = False


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "name", "city", "status", "library_module", "coworking_module", "created_at",
    )
    list_filter = ("status", "library_module", "coworking_module")
    search_fields = ("name", "slug", "city")
    readonly_fields = ("next_member_number",)
    prepopulated_fields = {"slug": ("name",)}
    actions = ["activate", "suspend"]

    def get_inlines(self, request, obj=None):
        # La politique n'est éditable que sur une structure déjà créée.
        return [LoanPolicyInline] if obj else []

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            LoanPolicy.objects.create(organization=obj)

    @admin.action(description="Activer les structures sélectionnées")
    def activate(self, request, queryset):
        updated = queryset.update(
            status=Organization.Status.ACTIVE, updated_at=timezone.now()
        )
        self.message_user(
            request, f"{updated} structure(s) activée(s).", messages.SUCCESS
        )

    @admin.action(description="Suspendre les structures sélectionnées")
    def suspend(self, request, queryset):
        updated = queryset.update(
            status=Organization.Status.SUSPENDED, updated_at=timezone.now()
        )
        self.message_user(
            request, f"{updated} structure(s) suspendue(s).", messages.SUCCESS
        )


@admin.register(OrganizationStaff)
class OrganizationStaffAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "active")
    list_filter = ("organization", "active")
    search_fields = ("user__email", "organization__name")
    list_select_related = ("user", "organization")
    autocomplete_fields = ("user", "organization")

    def get_readonly_fields(self, request, obj=None):
        # Une fois le rattachement créé, on ne change plus ni l'utilisateur ni la structure.
        return ("user", "organization") if obj else ()