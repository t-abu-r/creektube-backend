from django.urls import path
from .views import *

urlpatterns = [
    path("logingetvideo/", LoginGetVideo.as_view(), name="LoginGetVideo"),
    path("guestgetvideo/", GuestGetVideo.as_view(), name="GuestGetVideo"),
    path("watchvideo/", LoginWatchVideo.as_view(), name="WatchVideo"),
    path("guestwatchvideo/", GuestWatchVideo.as_view(), name="GuestWatchVideo"),
    path("getownvideo/", GetOwnVideo.as_view(), name="GetOwnVideo"),
    path("uploadvideo/", UploadVideo.as_view(), name="UploadVideo"),
    path("uploadcommentvideo/", UploadCommentVideo.as_view(), name="UploadCommentVideo"),
    path("comment/", CommentVideo.as_view(), name="CommentVideo"),
    path("mod-panel/", ModPanel.as_view()),
    path("getmod/", IfModerator.as_view()),
    path("searchvideo/", SearchVideo.as_view(), name="SearchVideo"),
]