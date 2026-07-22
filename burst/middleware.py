import re
import os
from django.http import HttpResponse, StreamingHttpResponse, FileResponse


class RangeFileMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        range_header = request.META.get('HTTP_RANGE', '').strip()
        if not range_header or response.status_code != 200:
            return response

        match = re.match(r'^bytes=(\d+)-(\d+)?$', range_header)
        if not match:
            return response

        if isinstance(response, FileResponse) and hasattr(response, 'file_to_stream'):
            file_obj = response.file_to_stream
            try:
                if hasattr(file_obj, 'size'):
                    file_size = file_obj.size
                elif hasattr(file_obj, 'name') and os.path.exists(str(file_obj.name)):
                    file_size = os.path.getsize(str(file_obj.name))
                else:
                    return response
            except Exception:
                return response

            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1

            if start >= file_size or end >= file_size or start > end:
                res = HttpResponse(status=416)
                res['Content-Range'] = f'bytes */{file_size}'
                return res

            length = end - start + 1
            try:
                file_obj.seek(start)
            except Exception:
                return response

            def file_iterator(file_object, chunk_size=8192, total_length=length):
                bytes_left = total_length
                while bytes_left > 0:
                    read_size = min(chunk_size, bytes_left)
                    data = file_object.read(read_size)
                    if not data:
                        break
                    bytes_left -= len(data)
                    yield data

            content_type = response.headers.get('Content-Type', 'application/octet-stream') if hasattr(response, 'headers') else 'application/octet-stream'
            new_response = StreamingHttpResponse(
                file_iterator(file_obj),
                status=206,
                content_type=content_type
            )
            new_response['Content-Range'] = f'bytes {start}-{end}/{file_size}'
            new_response['Content-Length'] = str(length)
            new_response['Accept-Ranges'] = 'bytes'
            return new_response

        return response

