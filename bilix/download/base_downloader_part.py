import asyncio
from pathlib import Path
from typing import Union, List, Iterable, Tuple
import aiofiles
import httpx
import uuid
import random
import os
import cgi
from anyio import run_process
from pymp4.parser import Box
from bilix._handle import Handler
from bilix.download.base_downloader import BaseDownloader
from bilix.utils import req_retry, merge_files, path_check


class BaseDownloaderPart(BaseDownloader):
    def __init__(
            self,
            client: httpx.AsyncClient = None,
            browser: str = None,
            speed_limit: Union[float, int, None] = None,
            stream_retry: int = 5,
            progress=None,
            logger=None,
            # unique params
            part_concurrency: int = 10,
    ):
        """Base Async http Content-Range Downloader"""
        super(BaseDownloaderPart, self).__init__(
            client=client,
            browser=browser,
            stream_retry=stream_retry,
            speed_limit=speed_limit,
            progress=progress,
            logger=logger
        )
        self.part_concurrency = part_concurrency

    async def _pre_req(self, urls: List[Union[str, httpx.URL]]) -> Tuple[int, str]:
        # use GET instead of HEAD due to 404 bug https://github.com/HFrost0/bilix/issues/16
        res = await req_retry(self.client, urls[0], follow_redirects=True, headers={'Range': 'bytes=0-1'})
        total = int(res.headers['Content-Range'].split('/')[-1])
        # get filename
        if content_disposition := res.headers.get('Content-Disposition', None):
            key, pdict = cgi.parse_header(content_disposition)
            filename = pdict.get('filename', '')
        else:
            filename = ''
        # change origin url to redirected position to avoid twice redirect
        if res.history:
            urls[0] = res.url
        return total, filename

    async def get_media_clip(
            self,
            url_or_urls: Union[str, Iterable[str]],
            path: Path,
            time_range: Tuple[int, int],
            init_range: str,
            seg_range: str,
            task_id=None,
    ):
        """

        :param url_or_urls:
        :param path:
        :param time_range: (start_time, end_time)
        :param init_range: xxx-xxx
        :param seg_range: xxx-xxx
        :param task_id:
        :return:
        """
        upper = task_id is not None and self.progress.tasks[task_id].fields.get('upper', None)
        exist, path = path_check(path)
        if exist:
            if not upper:
                self.logger.info(f'[green]已存在[/green] {path.name}')
            return path

        urls = [url_or_urls] if isinstance(url_or_urls, str) else [url for url in url_or_urls]
        init_start, init_end = map(int, init_range.split('-'))
        seg_start, seg_end = map(int, seg_range.split('-'))
        res = await req_retry(self.client, urls[0], follow_redirects=True,
                              headers={'Range': f'bytes={seg_start}-{seg_end}'})
        container = Box.parse(res.content)
        assert container.type == b'sidx'
        start_time, end_time = time_range
        pre_time, pre_byte = 0, seg_end + 1
        inside = False
        parts = [(init_start, init_end)]
        total = init_end - init_start + 1
        s = 0
        for idx, ref in enumerate(container.references):
            if ref.reference_type != "MEDIA":
                self.logger.debug("not a media", ref)
                continue
            seg_duration = ref.segment_duration / container.timescale
            if not inside and start_time < pre_time + seg_duration:
                s = start_time - pre_time
                inside = True
            if inside and end_time < pre_time:
                break
            if inside:
                total += ref.referenced_size
                parts.append((pre_byte, pre_byte + ref.referenced_size - 1))
            pre_time += seg_duration
            pre_byte += ref.referenced_size
        if len(parts) == 1:
            raise Exception(f"time range <{start_time}-{end_time}> invalid for <{path.name}>")

        if task_id is not None:
            await self.progress.update(
                task_id,
                total=self.progress.tasks[task_id].total + total if self.progress.tasks[task_id].total else total)
        else:
            task_id = await self.progress.add_task(description=path.name, total=total)
        p_sema = asyncio.Semaphore(self.part_concurrency)

        async def get_seg(part_range: Tuple[int, int]):
            async with p_sema:
                return await self._get_file_part(urls, path=path, part_range=part_range, task_id=task_id)

        file_list = await asyncio.gather(*[get_seg(part_range) for part_range in parts])
        path_tmp = path.with_name(str(uuid.uuid4()))
        await merge_files(file_list, path_tmp)
        # fix time range
        cmd = ['ffmpeg', '-ss', str(s), '-t', str(end_time - start_time), '-i', str(path_tmp),
               '-codec', 'copy', '-loglevel', 'quiet', '-f', 'mp4', str(path)]
        await run_process(cmd)
        os.remove(path_tmp)
        if not upper:  # no upstream task
            await self.progress.update(task_id, visible=False)
            self.logger.info(f"[cyan]已完成[/cyan] {path.name}")
        return path

    async def get_file(self, url_or_urls: Union[str, Iterable[str]], path: Path,
                       url_name: bool = True, task_id=None) -> Path:
        """

        :param url_or_urls: file url or urls with backups
        :param path: file path
        :param url_name: if True, use filename from url, in this case, path should be a directory
        :param task_id: if not provided, a new progress task will be created
        :return: downloaded file path
        """
        urls = [url_or_urls] if isinstance(url_or_urls, str) else [url for url in url_or_urls]
        upper = task_id is not None and self.progress.tasks[task_id].fields.get('upper', None)

        if not url_name:
            exist, path = path_check(path)
            if exist:
                if not upper:
                    self.logger.info(f'[green]已存在[/green] {path.name}')
                return path

        total, req_filename = await self._pre_req(urls)

        if url_name:
            file_name = req_filename if req_filename else str(urls[0]).split('/')[-1].split('?')[0]
            path /= file_name
            exist, path = path_check(path)
            if exist:
                if not upper:
                    self.logger.info(f'[green]已存在[/green] {path.name}')
                return path

        if task_id is not None:
            await self.progress.update(
                task_id,
                total=self.progress.tasks[task_id].total + total if self.progress.tasks[task_id].total else total)
        else:
            task_id = await self.progress.add_task(description=path.name, total=total)
        part_length = total // self.part_concurrency
        cors = []
        for i in range(self.part_concurrency):
            start = i * part_length
            end = (i + 1) * part_length - 1 if i < self.part_concurrency - 1 else total - 1
            cors.append(self._get_file_part(urls, path=path, part_range=(start, end), task_id=task_id))
        file_list = await asyncio.gather(*cors)
        await merge_files(file_list, new_path=path)
        if not upper:
            await self.progress.update(task_id, visible=False)
            self.logger.info(f"[cyan]已完成[/cyan] {path.name}")
        return path

    async def _get_file_part(self, urls: List[Union[str, httpx.URL]], path: Path, part_range: Tuple[int, int],
                             task_id) -> Path:
        start, end = part_range
        part_path = path.with_name(f'{path.name}.{part_range[0]}{part_range[1]}')
        exist, part_path = path_check(part_path)
        if exist:
            downloaded = os.path.getsize(part_path)
            start += downloaded
            await self.progress.update(task_id, advance=downloaded)
        if start > end:
            return part_path  # skip already finished
        url_idx = random.randint(0, len(urls) - 1)

        for times in range(1 + self.stream_retry):
            try:
                async with \
                        self.client.stream("GET", urls[url_idx], follow_redirects=True,
                                           headers={'Range': f'bytes={start}-{end}'}) as r, \
                        self._stream_context(times), \
                        aiofiles.open(part_path, 'ab') as f:
                    r.raise_for_status()
                    if r.history:  # avoid twice redirect
                        urls[url_idx] = r.url
                    async for chunk in r.aiter_bytes(chunk_size=self.chunk_size):
                        await f.write(chunk)
                        await self.progress.update(task_id, advance=len(chunk))
                        await self._check_speed(len(chunk))
                break
            except (httpx.HTTPStatusError, httpx.TransportError):
                continue
        else:
            raise Exception(f"STREAM 超过重复次数 {part_path.name}")
        return part_path


@Handler.register(name="Part")
def handle(kwargs):
    method = kwargs['method']
    if method == 'f' or method == 'get_file':
        return BaseDownloaderPart, BaseDownloaderPart.get_file
