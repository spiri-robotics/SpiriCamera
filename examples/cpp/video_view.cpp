// Writes decodable H.264 to a file: video_view <camera> <zenoh config> <out.h264>
#include <chrono>
#include <fstream>
#include <iostream>
#include <mutex>
#include <thread>

#include <nlohmann/json.hpp>

#include "zenoh.hxx"

using namespace zenoh;
using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

// Which packets can be decoded, and how many went missing.
class VideoSync {
   public:
    bool accept(bool keyframe, uint64_t sequence, uint64_t keyframe_sequence) {
        if (keyframe) {
            if (synced_ && sequence <= last_) return false;  // A late duplicate.
            if (last_ && sequence > last_ + 1) lost_ += sequence - last_ - 1;
            synced_ = true;
            keyframe_sequence_ = last_ = sequence;
            ++received_;
            return true;
        }
        if (!synced_ || keyframe_sequence < keyframe_sequence_ || sequence <= last_) return false;
        if (keyframe_sequence != keyframe_sequence_ || sequence != last_ + 1) {
            lost_ += sequence - last_ - 1;  // A gap: nothing decodes until a keyframe.
            last_ = sequence;
            synced_ = false;
            return false;
        }
        last_ = sequence;
        ++received_;
        return true;
    }
    bool synced() const { return synced_; }
    double take_fraction_lost() {
        const uint64_t expected = received_ + lost_;
        const double fraction = expected ? double(lost_) / expected : 0.0;
        received_ = lost_ = 0;
        return fraction;
    }

   private:
    bool synced_ = false;
    uint64_t last_ = 0, keyframe_sequence_ = 0, received_ = 0, lost_ = 0;
};

int main(int argc, char** argv) {
    const std::string camera = argv[1];
    const std::string subscriber_id = "cpp-viewer";
    auto session = Session::open(Config::from_file(argv[2]));
    std::ofstream out(argv[3], std::ios::binary);

    // The two keys arrive on different threads, so share the state under a lock.
    std::mutex lock;
    VideoSync sync;
    bool want_keyframe = false;

    const auto on_packet = [&](const Sample& sample, bool keyframe) {
        auto attachment = sample.get_attachment();
        if (!attachment.has_value()) return;
        const auto info = json::parse(attachment->get().as_string());
        std::lock_guard<std::mutex> guard(lock);
        if (sync.accept(keyframe, info["sequence"], info["keyframe_sequence"])) {
            // Annex B H.264. Hand it to your decoder along with info["pts"] (90 kHz).
            const std::vector<uint8_t> payload = sample.get_payload().as_vector();
            out.write(reinterpret_cast<const char*>(payload.data()), payload.size());
        } else if (!sync.synced()) {
            want_keyframe = true;
        }
    };

    auto keyframes = session.declare_subscriber(
        KeyExpr(camera + "/transport_video/@data_keyframe"),
        [&](const Sample& sample) { on_packet(sample, true); }, closures::none);
    auto interframes = session.declare_subscriber(
        KeyExpr(camera + "/transport_video/@data_interframe"),
        [&](const Sample& sample) { on_packet(sample, false); }, closures::none);

    const auto query = [&](const std::string& name, const std::string& body) {
        Session::GetOptions options;
        if (!body.empty()) options.payload = Bytes(body);
        session.get(KeyExpr(camera + "/transport_video/" + name), "",
                    [](const Reply&) {}, closures::none, std::move(options));
    };

    query("request_keyframe", "");  // Don't wait up to keyframe_interval for the first.

    auto last_report = Clock::now();
    const auto end = last_report + std::chrono::seconds(6);
    while (Clock::now() < end) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        bool request = false, report = false;
        double lost = 0.0;
        {
            std::lock_guard<std::mutex> guard(lock);
            request = want_keyframe;
            want_keyframe = false;
            if (Clock::now() - last_report >= std::chrono::seconds(1)) {
                report = true;
                lost = sync.take_fraction_lost();
                last_report = Clock::now();
            }
        }
        if (request) query("request_keyframe", "");
        if (report) query("report", json{{"subscriber", subscriber_id}, {"fraction_lost", lost}}.dump());
    }

    // Leave rate control straight away rather than after subscriber_timeout.
    query("report", json{{"subscriber", subscriber_id}, {"left", true}}.dump());
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
}
