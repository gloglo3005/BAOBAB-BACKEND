from django.db import transaction

from .models import AuditLog


def log_action(*, organization, action, entity, user=None, description="", metadata=None):
    """Écrit une ligne du journal d'audit.

    À appeler DANS la transaction de l'action tracée : si l'action échoue,
    la ligne est annulée avec elle ; si l'écriture du journal échoue,
    l'action est annulée aussi. `user` reste vide pour une commande ou le
    système. `entity` est l'objet concerné (Loan, BookCopy, Member...).
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "log_action doit être appelé dans la transaction de l'action tracée."
        )

    return AuditLog.objects.create(
        organization=organization,
        user=user,
        action=AuditLog.Action(action),
        entity_type=type(entity).__name__,
        entity_id=str(entity.pk),
        description=description,
        metadata=metadata or {},
    )
