from __future__ import annotations

from functools import partial
from inspect import isawaitable
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Generic,
    Type,
    TypeVar,
    Union,
)

import rich.repr

from . import events
from ._callback import count_parameters, invoke
from ._types import MessageTarget

if TYPE_CHECKING:
    from .app import App
    from .widget import Widget

    Reactable = Union[Widget, App]

ReactiveType = TypeVar("ReactiveType")


class _NotSet:
    pass


_NOT_SET = _NotSet()

T = TypeVar("T")


@rich.repr.auto
class Reactive(Generic[ReactiveType]):
    """Reactive descriptor.

    Args:
        default (ReactiveType | Callable[[], ReactiveType]): A default value or callable that returns a default.
        layout (bool, optional): Perform a layout on change. Defaults to False.
        repaint (bool, optional): Perform a repaint on change. Defaults to True.
        init (bool, optional): Call watchers on initialize (post mount). Defaults to False.
        always_update (bool, optional): Call watchers even when the new value equals the old value. Defaults to False.
        compute (bool, optional): Run compute methods when attribute is changed. Defaults to True.
    """

    _reactives: TypeVar[dict[str, object]] = {}

    def __init__(
        self,
        default: ReactiveType | Callable[[], ReactiveType],
        *,
        layout: bool = False,
        repaint: bool = True,
        init: bool = False,
        always_update: bool = False,
        compute: bool = True,
    ) -> None:
        self._default = default
        self._layout = layout
        self._repaint = repaint
        self._init = init
        self._always_update = always_update
        self._run_compute = compute

    def __rich_repr__(self) -> rich.repr.Result:
        yield self._default
        yield "layout", self._layout
        yield "repaint", self._repaint
        yield "init", self._init
        yield "always_update", self._always_update
        yield "compute", self._run_compute

    @classmethod
    def init(
        cls,
        default: ReactiveType | Callable[[], ReactiveType],
        *,
        layout: bool = False,
        repaint: bool = True,
        always_update: bool = False,
        compute: bool = True,
    ) -> Reactive:
        """A reactive variable that calls watchers and compute on initialize (post mount).

        Args:
            default (ReactiveType | Callable[[], ReactiveType]): A default value or callable that returns a default.
            layout (bool, optional): Perform a layout on change. Defaults to False.
            repaint (bool, optional): Perform a repaint on change. Defaults to True.
            always_update (bool, optional): Call watchers even when the new value equals the old value. Defaults to False.
            compute (bool, optional): Run compute methods when attribute is changed. Defaults to True.

        Returns:
            Reactive: A Reactive instance which calls watchers or initialize.
        """
        return cls(
            default,
            layout=layout,
            repaint=repaint,
            init=True,
            always_update=always_update,
            compute=compute,
        )

    @classmethod
    def var(
        cls,
        default: ReactiveType | Callable[[], ReactiveType],
    ) -> Reactive:
        """A reactive variable that doesn't update or layout.

        Args:
            default (ReactiveType | Callable[[], ReactiveType]):  A default value or callable that returns a default.

        Returns:
            Reactive: A Reactive descriptor.
        """
        return cls(default, layout=False, repaint=False, init=False)

    def _initialize_reactive(self, obj: Reactable, name: str) -> None:
        """Initialized a reactive attribute on an object.

        Args:
            obj (Reactable): An object with reactive attributes.
            name (str): name of attribute.
        """
        internal_name = f"_reactive_{name}"
        if hasattr(obj, internal_name):
            # Attribute already has a value
            return
        compute_method = getattr(obj, f"compute_{name}", None)
        if compute_method is not None and self._init:
            default = getattr(obj, f"compute_{name}")()
        else:
            default_or_callable = self._default
            default = (
                default_or_callable()
                if callable(default_or_callable)
                else default_or_callable
            )
        setattr(obj, internal_name, default)
        if self._init:
            self._check_watchers(obj, name, default)

    @classmethod
    def _initialize_object(cls, obj: Reactable) -> None:
        """Set defaults and call any watchers / computes for the first time.

        Args:
            obj (Reactable): An object with Reactive descriptors
        """

        for name, reactive in obj._reactives.items():
            reactive._initialize_reactive(obj, name)

    @classmethod
    def _reset_object(cls, obj: object) -> None:
        """Reset reactive structures on object (to avoid reference cycles).

        Args:
            obj (object): A reactive object.
        """
        getattr(obj, "__watchers", {}).clear()
        getattr(obj, "__computes", []).clear()

    def __set_name__(self, owner: Type[MessageTarget], name: str) -> None:

        # Check for compute method
        if hasattr(owner, f"compute_{name}"):
            # Compute methods are stored in a list called `__computes`
            try:
                computes = getattr(owner, "__computes")
            except AttributeError:
                computes = []
                setattr(owner, "__computes", computes)
            computes.append(name)

        # The name of the attribute
        self.name = name
        # The internal name where the attribute's value is stored
        self.internal_name = f"_reactive_{name}"
        default = self._default
        setattr(owner, f"_default_{name}", default)

    def __get__(self, obj: Reactable, obj_type: type[object]) -> ReactiveType:
        _rich_traceback_omit = True

        self._initialize_reactive(obj, self.name)

        value: ReactiveType
        compute_method = getattr(self, f"compute_{self.name}", None)
        if compute_method is not None:
            old_value = getattr(obj, self.internal_name)
            value = getattr(obj, f"compute_{self.name}")()
            setattr(obj, self.internal_name, value)
            self._check_watchers(obj, self.name, old_value)
        else:
            value = getattr(obj, self.internal_name)
        return value

    def __set__(self, obj: Reactable, value: ReactiveType) -> None:
        _rich_traceback_omit = True

        self._initialize_reactive(obj, self.name)
        name = self.name
        current_value = getattr(obj, name)
        # Check for validate function
        validate_function = getattr(obj, f"validate_{name}", None)
        # Call validate
        if callable(validate_function):
            value = validate_function(value)
        # If the value has changed, or this is the first time setting the value
        if current_value != value or self._always_update:
            # Store the internal value
            setattr(obj, self.internal_name, value)

            # Check all watchers
            self._check_watchers(obj, name, current_value)

            if self._run_compute:
                self._compute(obj)

            # Refresh according to descriptor flags
            if self._layout or self._repaint:
                obj.refresh(repaint=self._repaint, layout=self._layout)

    @classmethod
    def _check_watchers(cls, obj: Reactable, name: str, old_value: Any):
        """Check watchers, and call watch methods / computes

        Args:
            obj (Reactable): The reactable object.
            name (str): Attribute name.
            old_value (Any): The old (previous) value of the attribute.
        """
        _rich_traceback_omit = True
        # Get the current value.
        internal_name = f"_reactive_{name}"
        value = getattr(obj, internal_name)

        async def await_watcher(awaitable: Awaitable) -> None:
            """Coroutine to await an awaitable returned from a watcher"""
            _rich_traceback_omit = True
            await awaitable
            # Watcher may have changed the state, so run compute again
            obj.post_message_no_wait(
                events.Callback(sender=obj, callback=partial(Reactive._compute, obj))
            )

        def invoke_watcher(
            watch_function: Callable, old_value: object, value: object
        ) -> bool:
            """Invoke a watch function.

            Args:
                watch_function (Callable): A watch function, which may be sync or async.
                old_value (object): The old value of the attribute.
                value (object): The new value of the attribute.

            Returns:
                bool: True if the watcher was run, or False if it was posted.
            """
            _rich_traceback_omit = True
            param_count = count_parameters(watch_function)
            if param_count == 2:
                watch_result = watch_function(old_value, value)
            elif param_count == 1:
                watch_result = watch_function(value)
            else:
                watch_result = watch_function()
            if isawaitable(watch_result):
                # Result is awaitable, so we need to await it within an async context
                obj.post_message_no_wait(
                    events.Callback(
                        sender=obj, callback=partial(await_watcher, watch_result)
                    )
                )
                return False
            else:
                return True

        watch_function = getattr(obj, f"watch_{name}", None)
        if callable(watch_function):
            invoke_watcher(watch_function, old_value, value)

        watchers: list[Callable] = getattr(obj, "__watchers", {}).get(name, [])
        for watcher in watchers:
            invoke_watcher(watcher, old_value, value)

    @classmethod
    def _compute(cls, obj: Reactable) -> None:
        """Invoke all computes.

        Args:
            obj (Reactable): Reactable object.
        """
        _rich_traceback_guard = True
        for compute in obj._reactives.keys():
            try:
                compute_method = getattr(obj, f"compute_{compute}")
            except AttributeError:
                continue
            current_value = getattr(obj, f"_reactive_{compute}")
            value = compute_method()
            setattr(obj, f"_reactive_{compute}", value)
            if value != current_value:
                cls._check_watchers(obj, compute, current_value)


class reactive(Reactive[ReactiveType]):
    """Create a reactive attribute.

    Args:
        default (ReactiveType | Callable[[], ReactiveType]): A default value or callable that returns a default.
        layout (bool, optional): Perform a layout on change. Defaults to False.
        repaint (bool, optional): Perform a repaint on change. Defaults to True.
        init (bool, optional): Call watchers on initialize (post mount). Defaults to True.
        always_update (bool, optional): Call watchers even when the new value equals the old value. Defaults to False.
    """

    def __init__(
        self,
        default: ReactiveType | Callable[[], ReactiveType],
        *,
        layout: bool = False,
        repaint: bool = True,
        init: bool = True,
        always_update: bool = False,
    ) -> None:
        super().__init__(
            default,
            layout=layout,
            repaint=repaint,
            init=init,
            always_update=always_update,
        )


class var(Reactive[ReactiveType]):
    """Create a reactive attribute (with no auto-refresh).

    Args:
        default (ReactiveType | Callable[[], ReactiveType]): A default value or callable that returns a default.
        init (bool, optional): Call watchers on initialize (post mount). Defaults to True.
    """

    def __init__(
        self,
        default: ReactiveType | Callable[[], ReactiveType],
        init: bool = True,
    ) -> None:
        super().__init__(
            default,
            layout=False,
            repaint=False,
            init=init,
        )


def watch(
    obj: Reactable,
    attribute_name: str,
    callback: Callable[[Any], object],
    init: bool = True,
) -> None:
    """Watch a reactive variable on an object.

    Args:
        obj (Reactable): The parent object.
        attribute_name (str): The attribute to watch.
        callback (Callable[[Any], object]): A callable to call when the attribute changes.
        init (bool, optional): True to call watcher initialization. Defaults to True.
    """

    if not hasattr(obj, "__watchers"):
        setattr(obj, "__watchers", {})
    watchers: dict[str, list[Callable]] = getattr(obj, "__watchers")
    watcher_list = watchers.setdefault(attribute_name, [])
    if callback in watcher_list:
        return
    watcher_list.append(callback)
    if init:
        current_value = getattr(obj, attribute_name, None)
        Reactive._check_watchers(obj, attribute_name, current_value)
