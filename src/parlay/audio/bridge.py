    def _schedule_disconnect(self) -> None:
        if self._on_disconnect is None or self._loop is None:
            return
        self._loop.create_task(self._on_disconnect())