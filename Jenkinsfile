pipeline {
    agent any

    environment {
        APP_NAME = 'sms-chatgpt'
        DAEMON_IMAGE_NAME = "em22435/sms-chatgpt-daemon".toLowerCase()
        WORKER_IMAGE_NAME = "em22435/sms-chatgpt-worker".toLowerCase()
        IMAGE_TAG = "${env.BUILD_NUMBER}"
        PYTHON_BIN = 'python3'

        DOCKER_CREDENTIALS = 'docker-hub-credentials'
        OPENAI_API_KEY_CREDENTIALS = 'openai-api-key'

        KUBE_CA_CERT = 'minikube-ca-cert'
        KUBE_CLUSTER = 'minikube'
        KUBE_CONTEXT = 'minikube'
        KUBE_CREDENTIALS = 'minikube-jenkins-secret'
        KUBE_NAMESPACE = 'dev'
        KUBE_SERVER_URL = 'https://192.168.49.2:8443'
    }

    stages {
        stage('Install') {
            steps {
                sh '${PYTHON_BIN} --version'
                sh '${PYTHON_BIN} -m venv .venv'
                sh '. .venv/bin/activate && python -m pip install --upgrade pip && python -m pip install -e .'
            }
        }

        stage('Test') {
            steps {
                sh '. .venv/bin/activate && python -m unittest discover -s tests'
            }
        }

        stage('Build Images') {
            steps {
                script {
                    daemonImage = docker.build("${DAEMON_IMAGE_NAME}:${IMAGE_TAG}", "-f Dockerfile.daemon .")
                    workerImage = docker.build("${WORKER_IMAGE_NAME}:${IMAGE_TAG}", "-f Dockerfile .")
                }
            }
        }

        stage('Push to Docker Hub') {
            steps {
                script {
                    docker.withRegistry("", env.DOCKER_CREDENTIALS) {
                        daemonImage.push()
                        daemonImage.push("latest")
                        workerImage.push()
                        workerImage.push("latest")
                    }
                }
            }
        }

        stage('Cleanup') {
            steps {
                sh "docker rmi ${DAEMON_IMAGE_NAME}:${IMAGE_TAG} || true"
                sh "docker rmi ${DAEMON_IMAGE_NAME}:latest || true"
                sh "docker rmi ${WORKER_IMAGE_NAME}:${IMAGE_TAG} || true"
                sh "docker rmi ${WORKER_IMAGE_NAME}:latest || true"
            }
        }

        stage('Deploy to Sandbox') {
            when {
                branch 'dev'
            }
            environment {
                KUBE_NAMESPACE = 'sandbox'
            }
            steps {
                script {
                    deployToKubernetes()
                }
            }
        }

        stage('Deploy to Dev') {
            when {
                branch 'main'
            }
            environment {
                KUBE_NAMESPACE = 'dev'
            }
            steps {
                script {
                    deployToKubernetes()
                }
            }
        }

        stage('Promote to UAT') {
            when {
                branch 'main'
            }
            environment {
                KUBE_NAMESPACE = 'uat'
            }
            steps {
                input message: "Deploy version ${IMAGE_TAG} to UAT?", ok: 'Deploy to UAT'
                script {
                    deployToKubernetes()
                }
            }
        }

        stage('Promote to Prod') {
            when {
                branch 'main'
            }
            environment {
                KUBE_NAMESPACE = 'prod'
            }
            steps {
                input message: "Deploy version ${IMAGE_TAG} to Prod?", ok: 'Deploy to Prod'
                script {
                    deployToKubernetes()
                }
            }
        }
    }
}

def deployToKubernetes() {
    env.DEPLOYMENT_NAME = "${env.APP_NAME}-daemon"
    env.CONTAINER_NAME = 'daemon'
    env.NAMESPACE_NAME = env.KUBE_NAMESPACE
    env.DAEMON_IMAGE = "${env.DAEMON_IMAGE_NAME}:${env.IMAGE_TAG}"
    env.WORKER_IMAGE = "${env.WORKER_IMAGE_NAME}:${env.IMAGE_TAG}"

    withCredentials([string(credentialsId: env.OPENAI_API_KEY_CREDENTIALS, variable: 'OPENAI_API_KEY')]) {
        withKubeConfig(
            caCertificateId: env.KUBE_CA_CERT,
            clusterName: env.KUBE_CLUSTER,
            contextName: env.KUBE_CONTEXT,
            credentialsId: env.KUBE_CREDENTIALS,
            namespace: env.KUBE_NAMESPACE,
            restrictKubeConfigAccess: false,
            serverUrl: env.KUBE_SERVER_URL
        ) {
            sh 'envsubst --version'
            sh 'envsubst < k8s/namespace.yaml > prepared-namespace.yaml'
            sh 'envsubst < k8s/rbac.yaml > prepared-rbac.yaml'
            sh 'envsubst < k8s/configmap.yaml > prepared-configmap.yaml'
            sh 'envsubst < k8s/deployment.yaml > prepared-deploy.yaml'
            sh 'kubectl apply -f prepared-namespace.yaml'
            sh 'kubectl -n ${KUBE_NAMESPACE} create secret generic sms-chatgpt-secrets --from-literal=OPENAI_API_KEY="${OPENAI_API_KEY}" --dry-run=client -o yaml | kubectl apply -f -'
            sh 'kubectl apply -f prepared-rbac.yaml'
            sh 'kubectl apply -f prepared-configmap.yaml'
            sh 'kubectl apply -f prepared-deploy.yaml'
            sh 'kubectl -n ${KUBE_NAMESPACE} rollout status deployment/${DEPLOYMENT_NAME}'
        }
    }
}
