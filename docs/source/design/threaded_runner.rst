.. -*- mode: rst -*-
.. vi: set ft=rst sts=4 ts=4 sw=4 et tw=79:

.. _chap_threaded_runner:


****************
Threaded runner
****************

.. topic:: Specification scope and status

   This specification provides an overview over the current implementation.

Threads
=======

Datalad often requires the execution of subprocesses. While subprocesses are executed, datalad, i.e. its main thread, should be able to read data from stdout and stderr of the subprocess as well as write data to stdin of the subprocess. This requires a way to efficiently multiplex reading from stdout and stderr of the subprocess as well as writing to stdin of the subprocess.

Since non-blocking IO and waiting on multiple sources (poll or select) differs vastly in terms of capabilities and API on different OSs, we decided to use blocking IO and threads to multiplex reading from different sources.

Generally we have a number of threads that might be created and executed, depending on the need for writing to stdin or reading from stdout or stderr. Each thread can read from either a single queue or a file descriptor. Reading is done blocking. Each thread can put data into multiple queues. This is used to transport data that was read as well as for signaling conditions like closed file descriptors.

Conceptually, there are the main thread and two different types of threads:

 - type 1: transport threads (1 per process I/O descriptor)
 - type 2: timeout threads (only if timeout is not `None`: 1 per active I/O descriptor)

Transport Threads
.................

Besides the main thread, there might be up to three additional threads to handle data transfer to `stdin`, and from `stdout` and `stderr`. Each of those threads copies data between queues and file descriptors in a tight loop. The stdin-thread reads from an input-queue, the stdout- and stderr-threads write to an output queue. Each thread signals its exit to a set of signal queues, which might be identical to the output queues.

The `stdin`-thread reads data from a queue and writes it to the `stdin`-file descriptor of the sub-process. If it reads `None` from the queue, it will exit. The thread will also exit, if an exit is requested by calling `thread.request_exit()`, or if an error occurs during writing. In all cases it will enqueue a `None` to all its signal-queues.

The `stdout`- and `stderr`-threads read from the respective file descriptor and enqueue data into their output queue, unless the data has zero length (which indicates a closed descriptor). On a zero-length read they exit and enqueue `None` into their signal queues.

All queues are infinite. Nevertheless signaling is performed with a timeout of one 100 milli seconds in order to ensure that threads can exit.


Timeout Threads
...............

Timeout threads are created of the timeout-argument to `ThreadedRunner.run()` is not `None`. One timeout thread is created for each file descriptor that has a thread "attached". The timeout-threads consist of a single loop in wich the threads sleep for the specified timeout, using `time.sleep()`. After returning from `time.sleep()`, they check whether they have been reset by calling `time.sleep()` on them. If not, they will enqueue a timeout-message to their signal queues, again with a timeout of 100 milli seconds.


Main Thread
...........

There is a single queue, the `output_queue`, on which the main thread waits, after all threads are started. The `output_queue` is the signaling queue and the output queue of the stderr-thread and the stdout-thread. It is also the signaling queue of the stdin-thread, and it is the signaling queue for all timeout-threads.

The main thread waits on the `output_queue` for data or signals and handles them accordingly, i.e. calls data callbacks of the protocol if data arrives, calls timeout callbacks of the protocol if timeouts arrive, and calls connection-related callbacks of the protocol if other signals arrive. It also handles the closing of `stdin`-, `stdout`-, and `stderr`-file descriptors if the transport threads exit. These tasks are either done in the method `ThreadedRunner.run()` or in a result generator that is returned by  `ThreadedRunner.run()` whenever `send()` is called on it.


Protocols
=========

Due to its history the runner implementation uses the interface of the `SubprocessProtocol` (asyncio.protocols.SubprocessProtocol). Although the sub process protocol interface is defined in the asyncio libraries, the current thread-runner implementation does not make use of `async`.

    - `SubprocessProtocol.pipe_data_received(fd, data)`
    - `SubprocessProtocol.pipe_connection_lost(fd, exc)`
    - `SubprocessProtocol.process_exited()`

In addition the methods of `BaseProtocol` are called, i.e.:

    - `BaseProtocol.connection_made(transport)`
    - `BaseProtocol.connection_lost(exc)`


The datalad-provided protocol `WitlessProtocol` provides an additional callback:

    - `WitlessProtocol.timeout(fd)`

The method `timeout()` will be called when the parameter `timeout` in `WitlessRunner.run`, `ThreadedRunner.run`, or `run_command` is set to a number specifying the desired timeout in seconds. If no data is received from `stdin`, or `stderr` (if those are supposed to be captured) and no data could be written to `stdin` in the given timeout period, the method `WitlessProtocol.timeout(fd)` is called with `fd` set to the respective file number, e.g. 0, 1, or 2. If `WitlessProtocol.timeout(fd)` returns `True`, the file descriptor will be closed and the associated threads will exit.

The method `WitlessProtocol.timeout(fd)` is also called if all of stdout, stderr and stdin are closed and the process does not exit within the given interval. In this case `fd` is set to `None`. If `WitlessProtocol.timeout(fd)` returns `True` the process is terminated.


Object and Generator Results
================================

If the protocol that is provided to `run()` does not inherit `datalad.runner.protocol.GeneratorMixIn`, the final result that will be returned to the caller is determined by calling `WitlessProtocol._prepare_result()`. Whatever object this method returns will be returned to the caller.

If the protocol that is provided to `run()` does inherit `datalad.runner.protocol.GeneratorMixIn`, `run()` will return a `Generator`. This generator will yield the elements that were sent to it in the protocol-implementation by calling `GeneratorMixIn.send_result()` in the order in which the method `GeneratorMixIn.send_result()` is called. For example, if `GeneratorMixIn.send_result(43)` is called, the generator will yield `43`, and if `GeneratorMixIn.send_result({"a": 123, "b": "some data"})` is called, the generator will yield `{"a": 123, "b": "some data"}`.

Internally the generator is implemented by keeping track of the process state and waiting in the `output_queue` once, when `send` is called on it.
