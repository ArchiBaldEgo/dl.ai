(function () {
  function buildTopicOption(topic) {
    const option = document.createElement("option");
    option.value = String(topic.id);
    option.textContent = topic.topic_name;
    return option;
  }

  function buildPlaceholder(text) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = text;
    return option;
  }

  function fillTopics(topicSelect, topics, languageId, selectedTopicId) {
    topicSelect.innerHTML = "";
    if (!languageId) {
      topicSelect.appendChild(buildPlaceholder("Сначала выберите язык"));
      topicSelect.value = "";
      topicSelect.disabled = true;
      return;
    }

    topicSelect.appendChild(buildPlaceholder("---------"));
    const filteredTopics = topics
      .filter(function (topic) {
        return String(topic.programming_language) === String(languageId);
      })
      .sort(function (left, right) {
        return String(left.topic_name).localeCompare(String(right.topic_name), "ru");
      });

    filteredTopics.forEach(function (topic) {
      topicSelect.appendChild(buildTopicOption(topic));
    });

    const hasSelectedTopic = filteredTopics.some(function (topic) {
      return String(topic.id) === String(selectedTopicId);
    });
    topicSelect.value = hasSelectedTopic ? String(selectedTopicId) : "";
    topicSelect.disabled = false;
  }

  function initPromptLanguageTopicCascade() {
    const languageSelect = document.getElementById("id_programming_language");
    const topicSelect = document.getElementById("id_topic");
    if (!languageSelect || !topicSelect) {
      return;
    }

    const topicsUrl = languageSelect.dataset.topicsUrl || "/ai/api/topics/";
    const initialTopicId = topicSelect.value || "";

    fetch(topicsUrl, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Topics request failed");
        }
        return response.json();
      })
      .then(function (topics) {
        fillTopics(topicSelect, topics, languageSelect.value, initialTopicId);
        languageSelect.addEventListener("change", function () {
          fillTopics(topicSelect, topics, languageSelect.value, "");
        });
      })
      .catch(function () {
        // Topics API unreachable: keep the server-rendered <option>s intact so
        // the field stays usable. The server renders every topic when no
        // language is chosen (PromptForm.__init__), and clean() validates
        // topic<->language consistency server-side, so we only need to make
        // sure the select is not left disabled.
        topicSelect.disabled = false;
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPromptLanguageTopicCascade);
  } else {
    initPromptLanguageTopicCascade();
  }
})();
