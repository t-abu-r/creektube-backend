from django.urls import path
from .views import *

urlpatterns = [
    # GET VIDEO
    path("logingetvideo/", LoginGetVideo.as_view(), name="LoginGetVideo"),
    path("guestgetvideo/", GuestGetVideo.as_view(), name="GuestGetVideo"),
    path("watchvideo/", LoginWatchVideo.as_view(), name="WatchVideo"),
    path("guestwatchvideo/", GuestWatchVideo.as_view(), name="GuestWatchVideo"),
    path("searchvideo/", SearchVideo.as_view(), name="SearchVideo"),
    path("getownvideo/", GetOwnVideo.as_view(), name="GetOwnVideo"),

    # VIDEO/RELATED UPLOAD!!!!
    path("uploadvideo/", UploadVideo.as_view(), name="UploadVideo"),
    path("youtube/add/", AddYouTubeVideo.as_view(), name="AddYouTubeVideo"),
    path("uploadcommentvideo/", UploadCommentVideo.as_view(), name="UploadCommentVideo"),
    path("pikevideo/", PikeVideo.as_view(), name="PikeVideo"),
    path("dispikevideo/", DisPikeVideo.as_view(), name="DisPikeVideo"),
    path("creekaccount/", CreekAccount.as_view(), name="CreekAccount"),
    path("creek-youtube-channel/", CreekYouTubeChannel.as_view(), name="CreekYouTubeChannel"),
    path("youtube-follows/", YouTubeFollows.as_view(), name="YouTubeFollows"),


    path("categories/", Categories.as_view(), name="Categories"),
    path("categories/manage/", CategoryManage.as_view(), name="CategoryManage"),
    path("interests/<str:tag>/", InterestTag.as_view(), name="InterestTag"),
    path("comment/", CommentVideo.as_view(), name="CommentVideo"),
    path("pincomment/", PinComment.as_view(), name="PinComment"),
    path("youtube/channel/", YouTubeChannel.as_view(), name="YouTubeChannel"),
    # MOD PANEL
    path("mod-panel/", ModPanel.as_view()),
    path("set-account-active/", SetAccountActive.as_view(), name="SetAccountActive"),
    path("mod-logs/", ModActionLogs.as_view(), name="ModActionLogs"),
    path("getmod/", IfModerator.as_view(), name="GetMod"),
    path("mod-panel/searchusers/", SearchUsersMod.as_view(), name="GetModList"),

    # SUPERUSER ADMIN PANEL
    path("admin-panel/", AdminPanel.as_view(), name="AdminPanel"),
    path("admin-titles/", AdminTitles.as_view(), name="AdminTitles"),
    path("admin-users/", AdminUsers.as_view(), name="AdminUsers"),

    path("searchusers/", SearchUsers.as_view(), name="SearchUsers"),

    path("account/", Account.as_view(), name="GetAccount"),
    path("account/banner/", SetBanner.as_view(), name="SetBanner"),
    path("trackretention/", TrackRetention.as_view(), name="TrackRetention"),
    path("notifications/unread-count/", NotificationUnreadCount.as_view(), name="NotificationUnreadCount"),
    path("notifications/mark-read/", NotificationMarkRead.as_view(), name="NotificationMarkRead"),
    path("notifications/mark-all-read/", NotificationMarkAllRead.as_view(), name="NotificationMarkAllRead"),
    path("notifications/", NotificationList.as_view(), name="NotificationList"),
    path("comment-like/", CommentLikeToggle.as_view(), name="CommentLikeToggle"),
    path("comment-edit/", CommentEdit.as_view(), name="CommentEdit"),
    path("comment-delete/<int:comment_id>/", CommentDelete.as_view(), name="CommentDelete"),
    path("settings/", UserSettings.as_view(), name="UserSettings"),
    # Snips
    path("snip/upload/", UploadSnip.as_view(), name="UploadSnip"),
    path("snip/feed/", SnipFeed.as_view(), name="SnipFeed"),
    path("snip/watch/", WatchSnip.as_view(), name="WatchSnip"),
    path("snip/like/", LikeSnip.as_view(), name="LikeSnip"),
    path("snip/own/", GetOwnSnips.as_view(), name="GetOwnSnips"),
    path("snip/delete/<int:snip_id>/", SnipDelete.as_view(), name="SnipDelete"),
    path("snip/comments/", SnipCommentList.as_view(), name="SnipCommentList"),
    path("snip/comment/", UploadSnipComment.as_view(), name="UploadSnipComment"),
    path("snip/pincomment/", PinSnipComment.as_view(), name="PinSnipComment"),
    path("snip/studio/comments/", SnipStudioComments.as_view(), name="SnipStudioComments"),
    path("snip/trackretention/", TrackSnipRetention.as_view(), name="TrackSnipRetention"),


    # STUDIO
    path("studio/", Studio.as_view(), name="Studio"),
    path('studio/videos/<int:video_id>/', StudioVideoDelete.as_view(), name='studio-video-delete'),
    path("studio/comments/", StudioComments.as_view(), name="StudioComments"),
    path("analytics/", ChannelAnalytics.as_view(), name="ChannelAnalytics"),
    path("history/", WatchHistory.as_view(), name="WatchHistory"),
]
