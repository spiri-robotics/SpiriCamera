// Prints one line per raw frame: raw_view <camera> <zenoh config>
#include <chrono>
#include <iostream>
#include <thread>

#include <nlohmann/json.hpp>

#include "zenoh.hxx"

using namespace zenoh;
using json = nlohmann::json;

int main(int argc, char** argv) {
    const std::string camera = argv[1];
    auto session = Session::open(Config::from_file(argv[2]));

    // The exact key: a wildcard never matches an @ chunk.
    auto subscriber = session.declare_subscriber(
        KeyExpr(camera + "/transport_raw/@data"),
        [](const Sample& sample) {
            auto attachment = sample.get_attachment();
            if (!attachment.has_value()) return;
            const auto info = json::parse(attachment->get().as_string());
            const int width = info["width"], height = info["height"];
            // Tightly packed BGR, top row first: width * height * 3 bytes.
            const std::vector<uint8_t> pixels = sample.get_payload().as_vector();
            if (info["format"] != "bgr24" || pixels.size() != size_t(width) * height * 3) return;
            std::cout << width << "x" << height << " first pixel "
                      << int(pixels[0]) << "," << int(pixels[1]) << "," << int(pixels[2]) << "\n";
        },
        closures::none);

    // Only needed if the picture isn't changing, but always harmless.
    session.get(KeyExpr(camera + "/transport_raw/request_keyframe"), "",
                [](const Reply&) {}, closures::none);

    std::this_thread::sleep_for(std::chrono::seconds(3));
}
