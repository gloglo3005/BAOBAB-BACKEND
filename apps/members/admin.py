from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .models import LinkCode, Member
from .services import (
    LINK_CODE_VALIDITY_DAYS,
    accept_member,
    create_link_code,
    reactivate_member,
    reject_member,
    suspend_member,
)


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = (
        "member_number", "first_name", "last_name", "organization",
        "status", "has_account", "joined_at",
    )
    list_filter = ("organization", "status")
    search_fields = ("member_number", "first_name", "last_name", "phone", "user__email")
    readonly_fields = ("organization", "user", "member_number", "status", "joined_at")
    list_select_related = ("organization", "user")
    actions = ["accept", "reject", "suspend", "reactivate", "generate_link_code"]

    def has_add_permission(self, request):
        # Les membres se créent par les services (comptoir, demande, API).
        return False

    def has_delete_permission(self, request, obj=None):
        # On ne supprime pas un membre (RG14).
        return False

    @admin.display(boolean=True, description="Compte lié")
    def has_account(self, obj):
        return obj.user_id is not None

    def _apply(self, request, queryset, service, label):
        done = 0
        for member in queryset:
            try:
                service(member, performed_by=request.user)
                done += 1
            except ValidationError as error:
                self.message_user(
                    request, f"{member} : {error.messages[0]}", messages.ERROR
                )
        if done:
            self.message_user(request, f"{done} membre(s) {label}.", messages.SUCCESS)

    @admin.action(description="Accepter les membres sélectionnés")
    def accept(self, request, queryset):
        self._apply(request, queryset, accept_member, "accepté(s)")

    @admin.action(description="Refuser les membres sélectionnés")
    def reject(self, request, queryset):
        self._apply(request, queryset, reject_member, "refusé(s)")

    @admin.action(description="Suspendre les membres sélectionnés")
    def suspend(self, request, queryset):
        self._apply(request, queryset, suspend_member, "suspendu(s)")

    @admin.action(description="Réactiver les membres sélectionnés")
    def reactivate(self, request, queryset):
        self._apply(request, queryset, reactivate_member, "réactivé(s)")

    @admin.action(description="Générer un code de rattachement")
    def generate_link_code(self, request, queryset):
        for member in queryset:
            try:
                link_code = create_link_code(member=member, created_by=request.user)
            except ValidationError as error:
                self.message_user(
                    request, f"{member} : {error.messages[0]}", messages.ERROR
                )
            else:
                self.message_user(
                    request,
                    f"{member} : code {link_code.code} "
                    f"(valable {LINK_CODE_VALIDITY_DAYS} jours).",
                    messages.SUCCESS,
                )


@admin.register(LinkCode)
class LinkCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "member", "organization", "expires_at", "used_at")
    list_filter = ("organization",)
    search_fields = ("code", "member__member_number")
    list_select_related = ("organization", "member")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False