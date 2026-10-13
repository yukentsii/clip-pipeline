from clip_pipeline.publish.uploaders.base import BaseUploader, UploadError, UploadJob, UploadResult


def default_uploaders(settings=None) -> dict[str, BaseUploader]:
    from clip_pipeline.publish.uploaders.instagram import InstagramUploader
    from clip_pipeline.publish.uploaders.tiktok import TikTokUploader
    from clip_pipeline.publish.uploaders.youtube import YouTubeUploader

    return {u.platform: u for u in (YouTubeUploader(settings), InstagramUploader(settings), TikTokUploader(settings))}


__all__ = ["BaseUploader", "UploadError", "UploadJob", "UploadResult", "default_uploaders"]
