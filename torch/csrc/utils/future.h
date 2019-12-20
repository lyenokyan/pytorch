#pragma once

#include <ATen/core/ivalue.h>
#include <torch/csrc/autograd/profiler.h>

namespace torch {

namespace utils {

// FutureError inherits from std::exception, it can return const char* or
// std::string error message
class TORCH_API FutureError final : public std::exception {
 public:
  FutureError(std::string errorMsg) : errorMsg_(std::move(errorMsg)) {}

  FutureError() = default;

  const char* what() const noexcept override {
    return errorMsg_.c_str();
  }

 private:
  std::string errorMsg_;
};

// This class holds a value of type T that will be ready in the future.
// Most implementation is copied from FutureMessage and
// c10::ivalue::Future
template <typename T>
class TORCH_API Future final {
 public:
  using Callback =
      std::function<void(const T&, const c10::optional<FutureError>&)>;

  Future() = default;

  Future(T value) : completed_(true), value_(std::move(value)), rf_(nullptr) {}

  const T& wait() {
    std::unique_lock<std::mutex> lock(mutex_);
    finished_cv_.wait(lock, [this] { return completed_.load(); });

    if (error_) {
      throw *error_;
    }
    return value_;
  }

  const T& waitNoThrow() {
    std::unique_lock<std::mutex> lock(mutex_);
    finished_cv_.wait(lock, [this] { return completed_.load(); });
    return value_;
  }

  T&& moveValue() && {
    std::unique_lock<std::mutex> lock(mutex_);
    return std::move(value_);
  }

  void markCompleted(T value) {
    std::unique_lock<std::mutex> lock(mutex_);
    TORCH_CHECK(!completed());
    // Set value first as completed_ is accessed without lock
    value_ = std::move(value);
    completed_ = true;

    // Move callbacks to a vector on the stack so we can access it without
    // holding a lock
    std::vector<Callback> cbs;
    cbs.swap(callbacks_);
    lock.unlock();
    // if recording, run end callbacks.
    if (rf_) {
      rf_->end();
    }
    // There is no need to protect callbacks_ with the lock.
    // Once completed_ is set to true, no one can add new callback to the
    // list. pass value_, error_ for callback to easily check state.
    for (auto& callback : cbs) {
      callback(value_, error_);
    }
    finished_cv_.notify_all();
  }

  void setError(std::string errorMsg) {
    std::unique_lock<std::mutex> lock(mutex_);
    TORCH_CHECK(!completed());
    // Set error first as completed_ is accessed without lock
    error_ = FutureError(std::move(errorMsg));
    completed_ = true;

    // Move callbacks to a vector on the stack so we can access it without
    // holding a lock
    std::vector<Callback> cbs;
    cbs.swap(callbacks_);
    lock.unlock();
    // There is no need to protect callbacks_ with the lock.
    // Once completed_ is set to true, no one can add new callback to the
    // list. pass value_, error_ for callback to easily check state.
    for (auto& callback : cbs) {
      callback(value_, error_);
    }
    finished_cv_.notify_all();
  }

  bool completed() const {
    return completed_;
  }

  bool hasError() const {
    std::unique_lock<std::mutex> lock(mutex_);
    return error_ ? true : false;
  }

  c10::optional<FutureError> error() const {
    std::unique_lock<std::mutex> lock(mutex_);
    return error_;
  }

  // If completed() the callback will be invoked in-place.
  void addCallback(const Callback& callback) {
    std::unique_lock<std::mutex> lock(mutex_);
    if (completed()) {
      lock.unlock();
      callback(value_, error_);
      return;
    }
    callbacks_.push_back(callback);
  }
  // Attach a RecordFunction shared_ptr to this Future, to
  // persist the lifetime of the RecordFunction for the duration of the future.
  // This allows the future to control when this RecordFunction's callbacks are
  // run, ensuring that the RPC the future is associated with is profiled
  // appropriately.
  void attachRecordFunction(
      std::shared_ptr<torch::autograd::profiler::RecordFunction> rf) {
    rf_ = std::move(rf);
  }

 private:
  mutable std::mutex mutex_;
  std::atomic_bool completed_{false}; // is this future complete
  std::condition_variable finished_cv_;
  std::vector<Callback> callbacks_;
  T value_;
  c10::optional<FutureError> error_;
  std::shared_ptr<torch::autograd::profiler::RecordFunction> rf_;
};

} // namespace utils
} // namespace torch
