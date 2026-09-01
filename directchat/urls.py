from django.urls import path

from . import views

app_name = 'directchat'

urlpatterns = [
    # API endpoints for Next.js
    path('api/users/', views.UserListView.as_view(), name='api-user-list'),
    path('api/conversations/', views.ConversationsView.as_view(), name='api-conversations'),
    path('api/chat/<int:user2_pk>/history/', views.ChatHistoryView.as_view(), name='api-chat-history'),
]
