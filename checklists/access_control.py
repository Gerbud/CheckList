from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.urls import reverse

from checklists.models import (
    EmployeeProfile,
    Store,
    StoreTerminalAccount,
    UserStoreMembership,
)


DIRECTOR_STORE_SESSION_KEY = 'director_portal_store_id'


def _active_profile(user):
    if not getattr(user, 'is_authenticated', False) or not user.is_active:
        return None
    try:
        return EmployeeProfile.objects.select_related('store').get(
            user=user,
            is_active=True,
        )
    except EmployeeProfile.DoesNotExist:
        return None


def get_user_role(user):
    if not getattr(user, 'is_authenticated', False) or not user.is_active:
        return None
    if user.is_superuser:
        return EmployeeProfile.Role.SYSTEM_ADMIN
    profile = _active_profile(user)
    if profile is None:
        return None
    if profile.role == EmployeeProfile.Role.SYSTEM_ADMIN:
        return profile.role if profile.store_id is None else None
    if profile.role in {
        EmployeeProfile.Role.STORE_ACCOUNT,
        EmployeeProfile.Role.STORE_DIRECTOR,
    }:
        if profile.store_id and profile.store.is_active:
            return profile.role
    return None


def get_user_store(user):
    role = get_user_role(user)
    if role not in {
        EmployeeProfile.Role.STORE_ACCOUNT,
        EmployeeProfile.Role.STORE_DIRECTOR,
    }:
        return None
    return _active_profile(user).store


def is_store_account(user):
    if get_user_role(user) != EmployeeProfile.Role.STORE_ACCOUNT:
        return False
    store = get_user_store(user)
    return StoreTerminalAccount.objects.filter(
        user=user,
        store=store,
        is_active=True,
    ).exists()


def is_store_director(user):
    if get_user_role(user) == EmployeeProfile.Role.STORE_DIRECTOR:
        return True
    if not getattr(user, 'is_authenticated', False) or not user.is_active:
        return False
    return UserStoreMembership.objects.filter(
        user=user,
        is_active=True,
        store__is_active=True,
        role_in_store__in=(
            UserStoreMembership.Role.DIRECTOR,
            UserStoreMembership.Role.ADMINISTRATOR,
        ),
    ).exists()


def is_system_admin(user):
    return get_user_role(user) == EmployeeProfile.Role.SYSTEM_ADMIN


def can_access_store_terminal(user, store=None):
    if not is_store_account(user):
        return False
    return store is None or get_user_store(user).pk == store.pk


def can_access_director_portal(user, store=None):
    if is_system_admin(user):
        return store is not None and store.is_active
    if not is_store_director(user):
        return False
    if store is None:
        return True
    if UserStoreMembership.objects.filter(
        user=user,
        store=store,
        is_active=True,
        role_in_store__in=(
            UserStoreMembership.Role.DIRECTOR,
            UserStoreMembership.Role.ADMINISTRATOR,
        ),
    ).exists():
        return True
    own_store = get_user_store(user)
    return own_store is not None and own_store.pk == store.pk


def can_access_system_admin_portal(user):
    return is_system_admin(user)


def _can_manage_store(user, store):
    return can_access_director_portal(user, store)


can_manage_store_employees = _can_manage_store
can_manage_store_questions = _can_manage_store
can_manage_store_schedule = _can_manage_store
can_manage_store_notifications = _can_manage_store
can_manage_store_shifts = _can_manage_store
can_manage_store_tasks = _can_manage_store
can_manage_store_telegram = _can_manage_store
can_view_store_reports = _can_manage_store
can_reopen_store_stage = _can_manage_store


def can_manage_store(user, store):
    return _can_manage_store(user, store)


def can_manage_system_users(user):
    return is_system_admin(user)


def can_manage_stores(user):
    return is_system_admin(user)


def get_portal_home_url(user):
    role = get_user_role(user)
    if role == EmployeeProfile.Role.SYSTEM_ADMIN:
        return reverse('checklists:system_admin_dashboard')
    if is_store_director(user):
        return reverse('checklists:director_dashboard')
    if role == EmployeeProfile.Role.STORE_ACCOUNT:
        return reverse('checklists:terminal_home')
    # Telegram-only users do not have a separate web cabinet. The generic
    # landing page is safe and never sends them to the director portal.
    return reverse('checklists:dashboard')


def get_post_login_redirect(user):
    role = get_user_role(user)
    if is_store_director(user):
        return get_portal_home_url(user)
    if role in {
        EmployeeProfile.Role.STORE_ACCOUNT,
        EmployeeProfile.Role.STORE_DIRECTOR,
        EmployeeProfile.Role.SYSTEM_ADMIN,
    }:
        return get_portal_home_url(user)
    return '/login/'


def get_director_store(request):
    return resolve_managed_store(request)


def resolve_managed_store(request):
    """Resolve a managed Store without trusting URL, GET or POST values."""
    if is_store_director(request.user):
        memberships = UserStoreMembership.objects.filter(
            user=request.user,
            is_active=True,
            store__is_active=True,
            role_in_store__in=(
                UserStoreMembership.Role.DIRECTOR,
                UserStoreMembership.Role.ADMINISTRATOR,
            ),
        ).select_related('store')
        selected_id = request.session.get(DIRECTOR_STORE_SESSION_KEY)
        if selected_id:
            selected = memberships.filter(store_id=selected_id).first()
            if selected:
                return selected.store
        primary = get_user_store(request.user)
        if primary and (
            memberships.filter(store=primary).exists()
            or not memberships.exists()
        ):
            return primary
        membership = memberships.order_by('store__name', 'store_id').first()
        return membership.store if membership else primary
    if is_system_admin(request.user):
        store_id = request.session.get(DIRECTOR_STORE_SESSION_KEY)
        if store_id:
            return Store.objects.filter(pk=store_id, is_active=True).first()
    return None


def set_managed_store(request, store):
    if not store.is_active:
        raise ValueError('Нельзя выбрать неактивный магазин.')
    if not is_system_admin(request.user) and not UserStoreMembership.objects.filter(
        user=request.user,
        store=store,
        is_active=True,
        role_in_store__in=(
            UserStoreMembership.Role.DIRECTOR,
            UserStoreMembership.Role.ADMINISTRATOR,
        ),
    ).exists():
        raise PermissionError('Нет прав на выбранный магазин.')
    request.session[DIRECTOR_STORE_SESSION_KEY] = store.pk


def store_account_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not can_access_store_terminal(request.user):
            return HttpResponseForbidden('Доступен только аккаунту магазина.')
        return view(request, *args, **kwargs)

    return login_required(wrapped)


def store_director_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        store = get_director_store(request)
        if store is None or not can_access_director_portal(request.user, store):
            return HttpResponseForbidden('Доступ к кабинету магазина запрещён.')
        request.current_store = store
        return view(request, *args, **kwargs)

    return login_required(wrapped)


def system_admin_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not can_access_system_admin_portal(request.user):
            return HttpResponseForbidden('Доступен только администратору системы.')
        return view(request, *args, **kwargs)

    return login_required(wrapped)
